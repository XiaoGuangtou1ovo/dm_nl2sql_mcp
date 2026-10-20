"""MCP stdio 服务：可独立启动，也可由 LangChain 客户端管理子进程。"""

import argparse
import asyncio
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dm_core.config import AppError, Settings, configure_console
from dm_core.service import QueryService


def build_server(settings: Settings) -> FastMCP:
    service = QueryService(settings)
    server = FastMCP(
        "dm-nl2sql",
        instructions=(
            "只读自然语言数据库查询。业务表不明确时先调用search_catalog获取候选dataset_id，"
            "再调用query_data；必须依据工具状态、来源和截断标记回答。"
        ),
    )
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @server.tool(annotations=annotations, structured_output=True)
    def list_datasets() -> dict[str, Any]:
        """查看当前服务可查询的业务数据集，不返回连接凭据。"""
        return service.list_datasets()

    @server.tool(annotations=annotations, structured_output=True)
    async def search_catalog(question: str, limit: int = 5) -> dict[str, Any]:
        """按自然语言查找相关业务表、字段证据和dataset_id，不读取业务数据。"""
        return await asyncio.to_thread(service.search_catalog, question, limit)

    @server.tool(annotations=annotations, structured_output=True)
    async def query_data(
        question: str, dataset_ids: list[str] | None = None, max_rows: int = 100
    ) -> dict[str, Any]:
        """用完整自然语言问题查询数据。返回状态、数据、来源和实际时间范围；错误不能当作空结果。"""
        return await asyncio.to_thread(service.query, question, dataset_ids, max_rows)

    return server


def main():
    configure_console()
    parser = argparse.ArgumentParser(description="启动达梦 NL2SQL MCP stdio 服务")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    settings = Settings.load(args.env_file)
    logging.basicConfig(stream=sys.stderr, level=settings.get("LOG_LEVEL", "WARNING"))
    if settings.boolean("MCP_AUTO_BOOTSTRAP", False):
        from dm_core.bootstrap import ensure_catalog_ready

        try:
            result = ensure_catalog_ready(settings)
            logging.getLogger(__name__).warning("MCP catalog startup check: %s", result["action"])
        except AppError as exc:
            logging.getLogger(__name__).error("MCP catalog bootstrap failed: %s", exc)
            raise SystemExit(1) from exc
    build_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
