"""独立管理型MCP：发现元数据、检查状态和维护目录，不暴露业务查询。"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dm_core.bootstrap import discover
from dm_core.catalog import source_material, split_table
from dm_core.config import AppError, Settings, configure_console, digest, read_json
from dm_core.database import Database
from dm_core.embeddings import EmbeddingClient


def catalog_status(settings: Settings) -> dict[str, Any]:
    try:
        root = settings.path("INDEX_DIR", "artifacts/index")
        version = (root / "CURRENT").read_text(encoding="ascii").strip()
        if len(version) != 32 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("索引指针无效")
        directory = root / version
        if not (directory / "index.faiss").is_file() or not (directory / "vectors.npy").is_file():
            raise ValueError("索引文件缺失")
        manifest = read_json(directory / "manifest.json")
        catalog = read_json(directory / "catalog.json")
        _, _, source_hash = source_material(settings)
        fingerprint = EmbeddingClient(settings).fingerprint
        if manifest["source_hash"] != source_hash or manifest["embedding"] != fingerprint:
            return {
                "status": "not_ready",
                "error_code": "INDEX_STALE",
                "message": "描述、选表、连接或Embedding配置已变化，请刷新目录",
            }
        if manifest["catalog_hash"] != digest(catalog):
            raise ValueError("目录指纹不一致")
        datasets = catalog.get("datasets", [])
        if not datasets:
            raise ValueError("目录为空")
        return {
            "status": "ready",
            "catalog_version": digest(catalog)[:12],
            "tables": len(datasets),
            "documents": len(manifest["records"]),
        }
    except AppError as exc:
        return {
            "status": "not_ready",
            "error_code": exc.code,
            "message": str(exc),
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "status": "not_ready",
            "error_code": "INDEX_INVALID",
            "message": str(exc),
        }


def _check_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError("CONFIRMATION_REQUIRED", "该操作会更新正式目录或索引，必须显式确认")


def _bounded_max(settings: Settings, value: int) -> int:
    limit = settings.integer("MCP_ADMIN_MAX_TABLES", 200)
    if not 1 <= value <= limit:
        raise AppError("CONFIG", f"max_tables必须在1～{limit}之间")
    return value


def _run_cli(settings: Settings, arguments: list[str]) -> dict[str, Any]:
    """管理操作放到独立CLI进程，隔离FAISS/DLL生命周期和长时间Embedding任务。"""
    timeout = settings.integer("MCP_ADMIN_OPERATION_TIMEOUT_SECONDS", 900)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "dm_core.cli",
                "--env-file",
                str(settings.env_file),
                *arguments,
            ],
            cwd=settings.root,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("ADMIN_TIMEOUT", "管理操作超时") from exc
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError("ADMIN_FAILED", "管理命令未返回有效JSON") from exc
    if not isinstance(payload, dict):
        raise AppError("ADMIN_FAILED", "管理命令返回格式无效")
    if completed.returncode and payload.get("status") != "error":
        raise AppError("ADMIN_FAILED", "管理命令异常退出")
    return payload


def build_admin_server(settings: Settings) -> FastMCP:
    database = Database(settings)
    server = FastMCP(
        "dm-admin-mcp",
        instructions=(
            "管理员专用达梦元数据工具。只读发现可直接调用；任何目录发布或索引构建"
            "必须先向用户说明影响并获得确认，然后传confirm=true。"
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    mutating = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    @server.tool(annotations=read_only, structured_output=True)
    async def get_catalog_status() -> dict[str, Any]:
        """检查索引是否存在、是否与当前描述和Embedding配置一致。"""
        # FAISS在Windows工作线程中加载可能导致进程退出时原生线程未回收。
        return catalog_status(settings)

    @server.tool(annotations=read_only, structured_output=True)
    async def discover_schema(
        schemas: list[str],
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        max_tables: int = 50,
        details: bool = False,
    ) -> dict[str, Any]:
        """发现可见表和注释。details=true时返回字段元数据，可能较大。"""
        _bounded_max(settings, max_tables)
        return await asyncio.to_thread(
            discover,
            database,
            schemas=schemas,
            include=include,
            exclude=exclude,
            max_tables=max_tables,
            details=details,
        )

    @server.tool(annotations=read_only, structured_output=True)
    async def inspect_table(schema: str, table: str) -> dict[str, Any]:
        """读取一张表的字段、类型、注释和真实主键，不读取业务行。"""
        split_table(f"{schema}.{table}")
        metadata = await asyncio.to_thread(database.metadata, [(schema, table)])
        return {"status": "success", "table": metadata[0]}

    @server.tool(annotations=mutating, structured_output=True)
    async def bootstrap_catalog(
        schemas: list[str] | None = None,
        tables: list[str] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        keep_existing: bool = False,
        max_tables: int = 50,
        build_index: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """生成描述并发布目录。首次调用应先用discover_schema确认范围。"""
        _check_confirmation(confirm)
        _bounded_max(settings, max_tables)
        arguments = ["catalog-bootstrap"]
        for schema in schemas or []:
            arguments.extend(["--schema", schema])
        if tables:
            arguments.extend(["--tables", ",".join(tables)])
        for pattern in include or []:
            arguments.extend(["--include", pattern])
        for pattern in exclude or []:
            arguments.extend(["--exclude", pattern])
        if keep_existing:
            arguments.append("--keep-existing")
        if not build_index:
            arguments.append("--no-index")
        arguments.extend(
            [
                "--max-tables",
                str(max_tables),
                "--inventory-output",
                "artifacts/schema-inventory.json",
            ]
        )
        return await asyncio.to_thread(_run_cli, settings, arguments)

    @server.tool(annotations=mutating, structured_output=True)
    async def refresh_catalog(confirm: bool = False) -> dict[str, Any]:
        """按当前tables.json重新核验元数据并同步索引，不扩大选表范围。"""
        _check_confirmation(confirm)
        return await asyncio.to_thread(
            _run_cli,
            settings,
            ["catalog-sync", "--json"],
        )

    return server


def main():
    configure_console()
    parser = argparse.ArgumentParser(description="启动达梦管理型MCP stdio服务")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    settings = Settings.load(args.env_file)
    if not settings.get("MCP_ADMIN_TOKEN"):
        raise SystemExit("请配置MCP_ADMIN_TOKEN后再启动管理型MCP")
    logging.basicConfig(stream=sys.stderr, level=settings.get("LOG_LEVEL", "WARNING"))
    build_admin_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
