"""直接验证业务MCP：先检索目录，再把候选dataset_id传给query_data。"""

import argparse
import asyncio
import json

from dm_core.config import Settings, configure_console
from dm_core.qa import connect


def payload(result) -> dict:
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [item.text for item in result.content if item.type == "text"]
    if len(texts) == 1:
        value = json.loads(texts[0])
        if isinstance(value, dict):
            return value
    raise RuntimeError("MCP返回格式无效")


async def run(
    settings: Settings, question: str, limit: int, max_rows: int, search_only: bool
) -> dict:
    async with connect(settings) as session:
        search = payload(
            await session.call_tool(
                "search_catalog",
                {"question": question, "limit": limit},
            )
        )
        if search.get("status") != "success" or not search.get("candidates"):
            return search
        if search_only:
            return search
        result = payload(
            await session.call_tool(
                "query_data",
                {
                    "question": question,
                    "dataset_ids": [search["candidates"][0]["dataset_id"]],
                    "max_rows": max_rows,
                },
            )
        )
        result["catalog_search"] = search
        return result


def main():
    configure_console()
    parser = argparse.ArgumentParser(description="达梦NL2SQL MCP直接调用示例")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--question", required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--max-rows", type=int, default=10)
    parser.add_argument("--search-only", action="store_true", help="只搜索目录，不执行SQL")
    args = parser.parse_args()
    result = asyncio.run(
        run(
            Settings.load(args.env_file),
            args.question,
            args.limit,
            args.max_rows,
            args.search_only,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
