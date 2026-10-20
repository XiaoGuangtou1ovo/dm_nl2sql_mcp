"""管理入口：Excel 维护描述，一条命令核验元数据、同步索引并启动问答。"""

import argparse
import json
import sys
from pathlib import Path

from dm_core.catalog import build_catalog, initialize, selected_tables, split_table
from dm_core.config import AppError, Settings, configure_console, write_json
from dm_core.database import Database
from dm_core.discovery import parse_table_list


def output_path(settings: Settings, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (settings.root / path).resolve()


def main():
    configure_console()
    parser = argparse.ArgumentParser(description="达梦NL2SQL核心版")
    parser.add_argument("--env-file", default=".env")
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("catalog-sync", help="更新描述、核验达梦元数据并重建FAISS")
    sync_parser.add_argument(
        "--excel",
        help="描述工作簿路径；也可在env中设置DESCRIPTION_EXCEL后省略此参数",
    )
    sync_parser.add_argument(
        "--json",
        action="store_true",
        help="忽略DESCRIPTION_EXCEL，直接使用configs/tables.json和描述JSON",
    )
    commands.add_parser("list-datasets")
    init = commands.add_parser("catalog-init", help="生成新表描述，保留已有人工说明")
    init.add_argument("--details", action="store_true", help="展开尚未描述的全部列名")
    tables = commands.add_parser("tables", help="查看当前账号在某模式下可见的真实表名")
    tables.add_argument("--schema", required=True)
    discover = commands.add_parser(
        "discover", help="自动发现可见表、表注释和字段，可先输出清单供确认"
    )
    discover.add_argument("--schema", action="append", required=True)
    discover.add_argument("--include", action="append", help="通配符，可重复，例如 BT_*")
    discover.add_argument("--exclude", action="append", help="排除通配符，可重复")
    discover.add_argument("--max-tables", type=int, default=200)
    discover.add_argument("--details", action="store_true", help="同时读取字段与主键")
    discover.add_argument("--output", default="artifacts/schema-inventory.json")
    bootstrap = commands.add_parser(
        "catalog-bootstrap", help="自动生成描述、核验元数据并构建FAISS索引"
    )
    bootstrap.add_argument("--schema", action="append", help="模式，可重复")
    bootstrap.add_argument("--tables", help="精确表名，逗号分隔；与通配符选表二选一")
    bootstrap.add_argument("--include", action="append", help="通配符，可重复，例如 BT_*")
    bootstrap.add_argument("--exclude", action="append")
    bootstrap.add_argument(
        "--keep-existing",
        action="store_true",
        help="保留当前tables.json中的表，并与本次结果合并",
    )
    bootstrap.add_argument("--max-tables", type=int, default=200)
    bootstrap.add_argument(
        "--no-index", action="store_true", help="只生成并发布描述，不调用Embedding"
    )
    bootstrap.add_argument(
        "--inventory-output",
        default="artifacts/schema-inventory.json",
        help="元数据清单输出路径；设为空字符串可关闭",
    )
    query = commands.add_parser("query", help="单独验证自然语言查询链路")
    query.add_argument("question")
    query.add_argument("--max-rows", type=int, default=10)
    args = parser.parse_args()
    try:
        settings = Settings.load(args.env_file)
        if args.command == "tables":
            result = {"tables": Database(settings).tables(args.schema)}
        elif args.command == "discover":
            from dm_core.bootstrap import discover

            result = discover(
                Database(settings),
                schemas=args.schema,
                include=args.include,
                exclude=args.exclude,
                max_tables=args.max_tables,
                details=args.details,
            )
            path = output_path(settings, args.output)
            write_json(path, result)
            result = {
                "status": "success",
                "schemas": result["schemas"],
                "table_count": result["table_count"],
                "output": str(path),
                "note": result["note"],
            }
        elif args.command == "catalog-bootstrap":
            from dm_core.bootstrap import bootstrap_catalog

            result = bootstrap_catalog(
                settings,
                Database(settings),
                schemas=args.schema,
                tables=parse_table_list(args.tables),
                include=args.include,
                exclude=args.exclude,
                keep_existing=args.keep_existing,
                max_tables=args.max_tables,
                build_index=not args.no_index,
                inventory_output=args.inventory_output,
            )
        elif args.command in {"catalog-init", "catalog-sync"}:
            if args.command == "catalog-init":
                tables = selected_tables(settings)
                metadata = Database(settings).metadata([split_table(table) for table in tables])
                result = initialize(settings, metadata)
                if not args.details:
                    # 表多时默认只显示数量；需要补列时可用 --details 查看具体列名。
                    for change in result["changes"]:
                        change["not_described_count"] = len(change.pop("not_described"))
            else:
                from dm_core.index import sync
                from dm_core.spreadsheet import (
                    persist_description_workbook,
                    read_workbook,
                )

                excel_value = (
                    None if args.json else (args.excel or settings.get("DESCRIPTION_EXCEL"))
                )
                excel_spec = None
                excel_summary = None
                if excel_value:
                    excel_path = Path(excel_value)
                    if not excel_path.is_absolute():
                        excel_path = settings.root / excel_path
                    excel_path = excel_path.resolve()
                    excel_spec = read_workbook(excel_path)
                    tables = list(excel_spec.descriptions)
                else:
                    tables = selected_tables(settings)
                metadata = Database(settings).metadata([split_table(table) for table in tables])
                catalog, source_hash = build_catalog(
                    settings,
                    metadata,
                    descriptions=excel_spec.descriptions if excel_spec else None,
                    ontology=excel_spec.ontology if excel_spec else None,
                )
                result = sync(settings, catalog, source_hash)
                if excel_spec:
                    excel_summary = persist_description_workbook(
                        settings, excel_spec.descriptions, excel_spec.ontology
                    )
                    excel_summary["source"] = str(excel_path)
                    result["excel"] = excel_summary
        else:
            from dm_core.service import QueryService

            service = QueryService(settings)
            result = (
                service.list_datasets()
                if args.command == "list-datasets"
                else service.query(args.question, max_rows=args.max_rows)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") in {"error", "denied"}:
            sys.exit(1)
    except (AppError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": getattr(exc, "code", "CONFIG"),
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
