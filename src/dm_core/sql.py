"""确定性 SQL 编译器：只有受限计划能产生 SQL，不执行模型提供的 SQL 文本。"""

import math
from dataclasses import dataclass
from dataclasses import field as data_field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from dm_core.catalog import Catalog
from dm_core.config import AppError
from dm_core.models import Column, QueryPlan, TimePolicy, TimeRange, identifier


def quote(name: str) -> str:
    identifier(name)
    return f'"{name}"'


@dataclass
class CompiledQuery:
    """SQL 不包含用户值；参数与 SQL 分开，只在数据库适配层绑定。"""

    sql: str
    params: list
    dataset_ids: list[str]
    limit: int
    time_range: dict | None
    output_columns: list[dict] = data_field(default_factory=list)


class Compiler:
    def __init__(self, catalog: Catalog, max_rows: int):
        self.catalog, self.max_rows = catalog, max_rows

    def compile(self, plan: QueryPlan, allowed_ids: list[str], now: datetime) -> CompiledQuery:
        """首版支持单主表及直接 many_to_one/one_to_one LEFT JOIN，拒绝隐式关联。"""
        if plan.status != "ready" or not plan.dataset_id:
            raise AppError("PLAN_INVALID", "查询计划缺少主数据集")
        if plan.dataset_id not in allowed_ids:
            raise AppError("PLAN_DENIED", "计划引用了候选范围之外的数据集", denied=True)
        base = self.catalog.get(plan.dataset_id)
        aliases = {base.id: "t0"}
        joins = []
        params = []

        def table(dataset_id: str) -> str:
            d = self.catalog.get(dataset_id)
            return f"{quote(d.physical.schema_name)}.{quote(d.physical.table)}"

        def ensure(dataset_id: str) -> str:
            if dataset_id not in allowed_ids:
                raise AppError("PLAN_DENIED", "关联数据集不在允许范围", denied=True)
            if dataset_id not in aliases:
                target = self.catalog.get(dataset_id)
                matches = [r for r in base.relations if r.target_dataset_id == dataset_id]
                if (
                    len(matches) != 1
                    or target.physical.datasource_id != base.physical.datasource_id
                ):
                    raise AppError("PLAN_DENIED", "仅支持已配置的同数据源直接唯一关联", denied=True)
                relation = matches[0]
                alias = f"t{len(aliases)}"
                aliases[dataset_id] = alias
                conditions = [
                    f"{quote('t0')}.{quote(local)} = {quote(alias)}.{quote(remote)}"
                    for local, remote in zip(
                        relation.local_fields, relation.target_fields, strict=True
                    )
                ]
                joins.append(
                    f"LEFT JOIN {table(dataset_id)} {quote(alias)} ON " + " AND ".join(conditions)
                )
            return aliases[dataset_id]

        def field(ref: str) -> tuple[str, Column]:
            parts = ref.split(".")
            if len(parts) != 2:
                raise AppError("PLAN_INVALID", "字段引用必须为 dataset_id.FIELD")
            dataset_id, name = parts
            d = self.catalog.get(dataset_id)
            column = next((f for f in d.fields if f.name == name), None)
            if column is None:
                raise AppError("PLAN_INVALID", "计划引用了不存在的字段")
            return f"{quote(ensure(dataset_id))}.{quote(name)}", column

        groups = [field(ref)[0] for ref in plan.group_by]
        if groups and not plan.metrics:
            raise AppError("PLAN_INVALID", "分组查询必须指定业务指标")
        if plan.metrics and plan.fields:
            raise AppError("PLAN_INVALID", "聚合查询请将输出维度放在 group_by，而非 fields")
        selected = []
        output_columns = []
        output_refs: dict[str, str] = {}
        refs = plan.group_by if plan.metrics else plan.fields
        if not refs and not plan.metrics:
            refs = [f"{base.id}.{f.name}" for f in base.fields[:10]]
        for ref in refs:
            expression, column = field(ref)
            alias = ref.replace(".", "__")
            output_refs[ref] = quote(alias)
            selected.append(f"{expression} AS {quote(alias)}")
            output_columns.append(
                {"name": alias, "type": column.physical_type, "business_name": column.business_name}
            )
        for ref in plan.metrics:
            parts = ref.split(".")
            if len(parts) != 2 or parts[0] != base.id:
                raise AppError("PLAN_INVALID", "指标必须属于主数据集，防止关联后统计重复")
            metric = next((m for m in base.metrics if m.id == parts[1]), None)
            if metric is None:
                raise AppError("PLAN_INVALID", "未定义的业务指标")
            expression, column = field(f"{base.id}.{metric.field}")
            aggregate = metric.aggregation
            expression = (
                f"COUNT(DISTINCT {expression})"
                if aggregate == "count_distinct"
                else f"{aggregate.upper()}({expression})"
            )
            alias = ref.replace(".", "__")
            output_refs[ref] = quote(alias)
            selected.append(f"{expression} AS {quote(alias)}")
            output_columns.append(
                {
                    "name": alias,
                    "type": "BIGINT"
                    if aggregate.startswith("count")
                    else "NUMBER"
                    if aggregate == "avg"
                    else column.physical_type,
                    "business_name": metric.name,
                    "unit": metric.unit,
                }
            )
        if len(output_refs) != len(selected):
            raise AppError("PLAN_INVALID", "输出字段或指标重复")

        where = []
        operators = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        for condition in plan.filters:
            expression, column = field(condition.field)
            if condition.op in {"is_null", "not_null"}:
                where.append(f"{expression} IS {'NOT ' if condition.op == 'not_null' else ''}NULL")
                continue
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            if condition.op == "in" and not 1 <= len(values) <= 100:
                raise AppError("PLAN_INVALID", "IN 条件需要 1～100 个值")
            if condition.op != "in" and len(values) != 1:
                raise AppError("PLAN_INVALID", "该操作仅接受单个值")
            converted = [self._value(column, value) for value in values]
            if condition.op == "in":
                where.append(f"{expression} IN ({','.join('?' for _ in converted)})")
            elif condition.op == "contains":
                if not isinstance(converted[0], str) or column.enum:
                    raise AppError("PLAN_INVALID", "contains 仅支持普通文本字段")
                text = converted[0].replace("!", "!!").replace("%", "!%").replace("_", "!_")
                converted = [f"%{text}%"]
                where.append(f"{expression} LIKE ? ESCAPE '!'")
            else:
                where.append(f"{expression} {operators[condition.op]} ?")
            params.extend(converted)

        resolved = None
        if plan.time_range:
            if not base.time_policy:
                raise AppError("PLAN_INVALID", "数据集没有配置时间口径")
            ref = plan.time_range.field or f"{base.id}.{base.time_policy.default_field}"
            if ref.split(".")[0] != base.id:
                raise AppError(
                    "PLAN_INVALID", "首版时间范围仅支持主数据集字段，避免混用不同时间口径"
                )
            expression, column = field(ref)
            if column.role not in {"event_time", "ingestion_time"}:
                raise AppError("PLAN_INVALID", "时间筛选必须使用配置的时间字段")
            start, end = resolve_time(plan.time_range, base.time_policy, now)
            where.extend([f"{expression} >= ?", f"{expression} < ?"])
            # 首版 DATE/TIMESTAMP 按目录时区解释，无时区值绑定到数据库。
            params.extend([start.replace(tzinfo=None), end.replace(tzinfo=None)])
            resolved = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "bounds": "[start,end)",
                "timezone": base.time_policy.timezone,
            }

        order = []
        for item in plan.order_by:
            if item.field in output_refs:
                expression = output_refs[item.field]
            elif not plan.metrics:
                expression = field(item.field)[0]
            else:
                raise AppError("PLAN_INVALID", "聚合排序只能引用输出维度或指标")
            order.append(f"{expression} {item.direction.upper()}")
        if not order and not plan.metrics:
            order = [f"{quote('t0')}.{quote(key)} ASC" for key in base.primary_key]
        limit = min(plan.limit, self.max_rows)
        sql = f"SELECT {', '.join(selected)} FROM {table(base.id)} {quote('t0')}"
        if joins:
            sql += " " + " ".join(joins)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if groups:
            sql += " GROUP BY " + ", ".join(groups)
        if order:
            sql += " ORDER BY " + ", ".join(order)
        # 多取一行检测截断；数据库 LIMIT 与 fetchmany 双重限制返回量。
        sql += f" LIMIT {limit + 1}"
        return CompiledQuery(sql, params, list(aliases), limit, resolved, output_columns)

    @staticmethod
    def _value(column: Column, value):
        """枚举标签映射为真实值；禁止把未知枚举或 NULL 作为普通比较值。"""
        if value is None:
            raise AppError("PLAN_INVALID", "NULL 筛选请使用 is_null/not_null")
        if isinstance(value, float) and not math.isfinite(value):
            raise AppError("PLAN_INVALID", "条件参数不能是非有限数值")
        if column.enum:
            match = next((e for e in column.enum if value == e.value or value == e.label), None)
            if match is None:
                raise AppError("PLAN_INVALID", f"{column.business_name} 的枚举值不存在")
            return match.value
        kind = column.physical_type.upper()
        if any(
            t in kind for t in ("INT", "NUMBER", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")
        ):
            if type(value) not in {int, float}:
                raise AppError("PLAN_INVALID", "数值字段需要数值参数")
        elif not isinstance(value, str):
            raise AppError("PLAN_INVALID", "文本或日期字段需要字符串参数")
        return value


def resolve_time(spec: TimeRange, policy: TimePolicy, now: datetime) -> tuple[datetime, datetime]:
    """支持最近 N 天、日/周及绝对范围；拒绝隐式服务器时区和倒置区间。"""
    zone = ZoneInfo(policy.timezone)
    current = now.astimezone(zone)
    midnight = datetime.combine(current.date(), time.min, zone)
    if spec.mode == "recent_days":
        days = spec.days or policy.recent_days
        if policy.recent_mode == "calendar_days":
            start, end = midnight - timedelta(days=days - 1), current
        else:
            start, end = current - timedelta(days=days), current
    elif spec.mode == "today":
        start, end = midnight, current
    elif spec.mode == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif spec.mode in {"this_week", "last_week"}:
        monday = midnight - timedelta(days=current.weekday())
        start, end = (
            (monday, current) if spec.mode == "this_week" else (monday - timedelta(days=7), monday)
        )
    else:

        def parse(value: str | None) -> datetime:
            if value is None:
                raise AppError("PLAN_INVALID", "绝对时间范围缺少起止值")
            try:
                result = datetime.fromisoformat(value)
                if len(value) == 10:
                    result = result.replace(tzinfo=zone)
                if result.tzinfo is None:
                    raise ValueError("缺少时区")
                return result.astimezone(zone)
            except ValueError as exc:
                raise AppError("PLAN_INVALID", "绝对时间必须为日期或携带时区的 ISO 时间") from exc

        start, end = parse(spec.start), parse(spec.end)
    if start >= end:
        raise AppError("PLAN_INVALID", "时间范围起点必须早于终点")
    return start, end


def validate_sql(query: CompiledQuery, catalog: Catalog) -> None:
    """仅验证首版 SELECT 子集；mysql 解析器用于 LIMIT/?，不代表完整达梦方言。"""
    try:
        trees = sqlglot.parse(query.sql, read="mysql")
    except ParseError as exc:
        raise AppError("SQL_INVALID", "生成 SQL 不在首版支持的语法范围") from exc
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise AppError("SQL_DENIED", "只允许单条 SELECT", denied=True)
    tree = trees[0]
    if tree.args.get("with_") or tree.args.get("into") or tree.args.get("locks"):
        raise AppError("SQL_DENIED", "不支持 CTE、INTO 或锁定查询", denied=True)
    if list(tree.find_all(exp.Subquery)) or list(tree.find_all(exp.Star)):
        raise AppError("SQL_DENIED", "不支持子查询和通配字段", denied=True)
    expected = {
        (catalog.get(key).physical.schema_name, catalog.get(key).physical.table)
        for key in query.dataset_ids
    }
    actual = {(t.db, t.name) for t in tree.find_all(exp.Table)}
    if actual != expected or any(t.catalog for t in tree.find_all(exp.Table)):
        raise AppError("SQL_DENIED", "SQL 表范围与计划不一致", denied=True)
    if any(isinstance(f, exp.Anonymous) for f in tree.find_all(exp.Func)):
        raise AppError("SQL_DENIED", "禁止未知函数", denied=True)
    if len(list(tree.find_all(exp.Placeholder))) != len(query.params):
        raise AppError("SQL_INVALID", "绑定参数数量不一致")
    if tree.args.get("limit") is None:
        raise AppError("SQL_DENIED", "查询必须带服务端行数限制", denied=True)
