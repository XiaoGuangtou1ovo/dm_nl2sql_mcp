"""达梦访问与独立工作进程：只读元数据/编译后的SELECT，硬超时后回收进程。

连接生命周期改造自CleanCodeStar/dm_mcp_server（530a5c4d）；保留项目MIT许可。
"""

import json
import math
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from dm_core.config import AppError, Settings
from dm_core.models import Dataset, identifier


@contextmanager
def get_connection(settings):
    try:
        import dmPython
    except ImportError as exc:
        raise AppError("DRIVER_MISSING", "请安装dameng依赖，并配置匹配的DPI/DLL") from exc
    if settings.required("DAMENG_USER").upper() in {"SYSDBA", "SYSSSO", "SYSAUDITOR"}:
        raise AppError("CONFIG", "请使用获准业务表的专用只读账号")
    conn = dmPython.connect(
        server=settings.required("DAMENG_HOST"),
        port=settings.integer("DAMENG_PORT", 5236),
        user=settings.required("DAMENG_USER"),
        password=settings.required("DAMENG_PASSWORD"),
        connection_timeout=settings.integer("QUERY_TIMEOUT_SECONDS", 30),
        local_code=dmPython.PG_UTF8,
    )
    try:
        yield conn
    finally:
        conn.close()


def describe(conn, schema, table):
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT C.COLUMN_NAME,C.DATA_TYPE,C.NULLABLE,M.COMMENTS FROM ALL_TAB_COLUMNS C "
            "LEFT JOIN ALL_COL_COMMENTS M ON C.OWNER=M.OWNER AND C.TABLE_NAME=M.TABLE_NAME "
            "AND C.COLUMN_NAME=M.COLUMN_NAME WHERE C.OWNER=? AND C.TABLE_NAME=? ORDER BY C.COLUMN_ID",
            (schema, table),
        )
        columns = [
            {"name": r[0], "type": r[1], "nullable": r[2] == "Y", "comment": r[3] or ""}
            for r in cursor.fetchall()
        ]
        if not columns:
            raise AppError(
                "TABLE_NOT_VISIBLE",
                f"{schema}.{table}不存在或当前账号不可见；用tables检查模式和表名",
            )
        cursor.execute(
            "SELECT COMMENTS FROM ALL_TAB_COMMENTS WHERE OWNER=? AND TABLE_NAME=?",
            (schema, table),
        )
        table_comment_row = cursor.fetchone()
        table_comment = table_comment_row[0] if table_comment_row else ""
        cursor.execute(
            "SELECT CC.COLUMN_NAME FROM ALL_CONSTRAINTS C JOIN ALL_CONS_COLUMNS CC "
            "ON C.OWNER=CC.OWNER AND C.CONSTRAINT_NAME=CC.CONSTRAINT_NAME "
            "WHERE C.OWNER=? AND C.TABLE_NAME=? AND C.CONSTRAINT_TYPE='P' ORDER BY CC.POSITION",
            (schema, table),
        )
        return {
            "schema": schema,
            "table": table,
            "table_comment": table_comment or "",
            "columns": columns,
            "primary_key": [r[0] for r in cursor.fetchall()],
        }
    finally:
        cursor.close()


def list_tables(conn, schemas):
    """一次读取多个模式中的表和表注释，供首次选表和建目录使用。"""
    if not schemas:
        raise AppError("CONFIG", "至少需要一个模式")
    cursor = conn.cursor()
    try:
        result = []
        # 达梦对多值IN绑定及跨系统视图DISTINCT的兼容性不一致，按模式复用已验证的绑定形式。
        for schema in schemas:
            cursor.execute(
                "SELECT OWNER,TABLE_NAME,COMMENTS FROM ALL_TAB_COMMENTS "
                "WHERE OWNER=? ORDER BY TABLE_NAME",
                (identifier(schema),),
            )
            result.extend(
                {"schema": row[0], "table": row[1], "table_comment": row[2] or ""}
                for row in cursor.fetchall()
            )
        return sorted(result, key=lambda row: (row["schema"], row["table"]))
    finally:
        cursor.close()


def normalize_type(value):
    value = value.upper().split("(")[0].strip()
    return {
        "INT": "INTEGER",
        "INT4": "INTEGER",
        "INT8": "BIGINT",
        "CHARACTER VARYING": "VARCHAR",
        "DEC": "DECIMAL",
    }.get(value, value)


def verify_dataset(dataset, metadata):
    actual = {c["name"]: c for c in metadata["columns"]}
    for column in dataset.fields:
        if column.name not in actual or normalize_type(column.physical_type) != normalize_type(
            actual[column.name]["type"]
        ):
            raise AppError(
                "SCHEMA_CHANGED",
                f"{dataset.physical.schema_name}.{dataset.physical.table}.{column.name}缺失或类型变化，请catalog-sync",
            )
    if dataset.primary_key != metadata["primary_key"]:
        raise AppError("SCHEMA_CHANGED", "主键发生变化，请catalog-sync")


def json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise AppError("RESULT_TYPE", "不支持LOB、二进制或非有限数值，请减少输出字段")


def run_task(settings, task):
    with get_connection(settings) as conn:
        if task["action"] == "describe":
            return [
                describe(conn, identifier(schema), identifier(table))
                for schema, table in task["tables"]
            ]
        if task["action"] == "list_tables":
            return list_tables(conn, task["schemas"])
        if task["action"] == "tables":
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT DISTINCT OWNER,TABLE_NAME FROM ALL_TAB_COLUMNS WHERE OWNER=? ORDER BY TABLE_NAME",
                    (identifier(task["schema"]),),
                )
                return [f"{r[0]}.{r[1]}" for r in cursor.fetchall()]
            finally:
                cursor.close()
        datasets = [Dataset.model_validate(d) for d in task["datasets"]]
        for d in datasets:
            verify_dataset(d, describe(conn, d.physical.schema_name, d.physical.table))
        cursor = conn.cursor()
        try:
            cursor.execute(task["sql"], tuple(task["params"]))
            logical = {c["name"]: c for c in task["output_columns"]}
            columns = [{"name": c[0], **logical.get(c[0], {})} for c in cursor.description]
            names = [c["name"] for c in columns]
            rows, used, truncated = [], 0, False
            for _ in range(task["limit"] + 1):
                row = cursor.fetchone()
                if row is None:
                    break
                item = dict(zip(names, [json_value(v) for v in row], strict=True))
                size = len(json.dumps(item, ensure_ascii=False).encode())
                if len(rows) >= task["limit"] or used + size > task["max_bytes"]:
                    truncated = True
                    break
                rows.append(item)
                used += size
            return {
                "data": rows,
                "columns": columns,
                "returned_rows": len(rows),
                "truncated": truncated,
            }
        finally:
            cursor.close()


def worker():
    def decode(item):
        if set(item) == {"__datetime__"}:
            return datetime.fromisoformat(item["__datetime__"])
        return item

    stage = "connect_or_execute"
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"), object_hook=decode)
        raw = request["settings"]
        settings = Settings(Path(raw["root"]), raw["values"], Path(raw["env_file"]))
        result = run_task(settings, request["task"])
        response = {"ok": True, "result": result}
    except AppError as exc:
        response = {"ok": False, "code": exc.code, "message": str(exc), "denied": exc.denied}
    except Exception as exc:
        # 不回传原始驱动异常：只提取数字错误码，避免密码、参数或连接串泄露。
        code = re.search(r"\[CODE:\s*(-?\d+)\]", str(exc))
        number = code.group(1) if code else None
        message = "用户名或密码错误" if number == "-2501" else "检查驱动、连接、权限或达梦SQL兼容性"
        response = {
            "ok": False,
            "code": "DATABASE_ERROR",
            "message": f"{message}；驱动码={number or '未知'}；阶段={stage}",
        }
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


class Database:
    def __init__(self, settings):
        self.settings = settings

    def _call(self, task):
        settings = self.settings
        payload = {
            "settings": {
                "root": str(settings.root),
                "values": settings.values,
                "env_file": str(settings.env_file),
            },
            "task": task,
        }

        def encode(value):
            if isinstance(value, datetime):
                return {"__datetime__": value.isoformat()}
            raise TypeError("不支持的内部参数类型")

        timeout = (
            settings.integer("METADATA_TIMEOUT_SECONDS", 120)
            if task["action"] in {"describe", "tables", "list_tables"}
            else settings.integer("QUERY_TIMEOUT_SECONDS", 30)
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "dm_core.database"],
                input=json.dumps(payload, ensure_ascii=False, default=encode).encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode:
                raise AppError("DATABASE_ERROR", "数据库工作进程异常退出，请检查Python与驱动环境")
            result = json.loads(completed.stdout.decode("utf-8"))
            if not result["ok"]:
                raise AppError(
                    result["code"], result["message"], denied=result.get("denied", False)
                )
            return result["result"]
        except subprocess.TimeoutExpired as exc:
            raise AppError("DATABASE_TIMEOUT", "数据库操作超时，执行进程已停止") from exc
        except (OSError, ValueError, KeyError) as exc:
            raise AppError("DATABASE_ERROR", "数据库工作进程通信失败") from exc

    def metadata(self, tables):
        return self._call({"action": "describe", "tables": tables})

    def tables(self, schema):
        return self._call({"action": "tables", "schema": schema})

    def list_tables(self, schemas):
        return self._call({"action": "list_tables", "schemas": list(schemas)})

    def execute(self, query, datasets):
        return self._call(
            {
                "action": "query",
                "sql": query.sql,
                "params": query.params,
                "limit": query.limit,
                "max_bytes": self.settings.integer("MAX_RESPONSE_BYTES", 262144),
                "output_columns": query.output_columns,
                "datasets": [d.model_dump() for d in datasets],
            }
        )


if __name__ == "__main__":
    worker()
