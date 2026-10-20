"""首次接库编排：发现元数据、生成说明、核验目录、建索引后原子发布配置。"""

from datetime import datetime, timezone
from pathlib import Path

from dm_core.catalog import (
    build_catalog,
    ontology_path,
    prepare_descriptions,
    selected_tables,
    split_table,
)
from dm_core.config import AppError, Settings, read_json, write_json
from dm_core.database import Database
from dm_core.discovery import filter_table_refs, parse_table_list, table_refs
from dm_core.models import identifier


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _existing_tables(settings: Settings) -> list[str]:
    path = settings.path("TABLES_PATH", "configs/tables.json")
    return selected_tables(settings) if path.exists() else []


def resolve_table_scope(
    settings: Settings,
    database,
    *,
    schemas: list[str] | None,
    tables: list[str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
    keep_existing: bool,
    max_tables: int,
) -> list[str]:
    """精确表名优先；否则必须给出模式与include，避免误把整库建成索引。"""
    existing = _existing_tables(settings) if keep_existing else []
    if tables:
        selected = list(dict.fromkeys([*existing, *tables]))
    else:
        if not schemas:
            raise AppError("CONFIG", "请用--tables精确选表，或同时提供--schema和--include")
        if not include:
            raise AppError("CONFIG", '按模式选表时必须提供--include，例如--include "BT_*"')
        normalized = list(dict.fromkeys(identifier(schema) for schema in schemas))
        selected = filter_table_refs(
            table_refs(database.list_tables(normalized)),
            include=include,
            exclude=exclude,
            max_tables=max_tables,
        )
        selected = list(dict.fromkeys([*existing, *selected]))
    for table in selected:
        split_table(table)
    if not selected:
        raise AppError("TABLE_SCOPE_EMPTY", "没有选择任何表")
    if len(selected) > max_tables:
        raise AppError(
            "TABLE_SCOPE_TOO_LARGE",
            f"合并后的选表数量{len(selected)}超过上限{max_tables}",
        )
    return selected


def discover(
    database,
    *,
    schemas: list[str],
    include: list[str] | None,
    exclude: list[str] | None,
    max_tables: int,
    details: bool,
) -> dict:
    normalized = list(dict.fromkeys(identifier(schema) for schema in schemas))
    rows = database.list_tables(normalized)
    selected = filter_table_refs(
        table_refs(rows),
        include=include,
        exclude=exclude,
        max_tables=max_tables,
    )
    by_table = {f"{row['schema']}.{row['table']}": row for row in rows}
    tables = [by_table[table] for table in selected]
    if details:
        tables = database.metadata([split_table(table) for table in selected])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schemas": normalized,
        "table_count": len(tables),
        "tables": tables,
        "note": "仅读取数据库元数据；确认范围后用catalog-bootstrap生成目录和索引。",
    }


def bootstrap_catalog(
    settings: Settings,
    database,
    *,
    schemas: list[str] | None,
    tables: list[str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
    keep_existing: bool,
    max_tables: int,
    build_index: bool,
    inventory_output: str | None,
) -> dict:
    selected = resolve_table_scope(
        settings,
        database,
        schemas=schemas,
        tables=tables,
        include=include,
        exclude=exclude,
        keep_existing=keep_existing,
        max_tables=max_tables,
    )
    metadata = database.metadata([split_table(table) for table in selected])
    descriptions, report = prepare_descriptions(settings, metadata, selected)
    path = ontology_path(settings)
    ontology = read_json(path) if path.exists() else {"entities": [], "relations": []}

    # 先在内存中完成描述和元数据核验，再允许索引构建或任何配置发布。
    catalog, source_hash = build_catalog(settings, metadata, descriptions, ontology)
    if build_index:
        from dm_core.index import sync

        index_result = sync(settings, catalog, source_hash)
    else:
        index_result = {"action": "skipped"}

    from dm_core.spreadsheet import persist_description_workbook

    published = persist_description_workbook(settings, descriptions, ontology)
    inventory_path = None
    if inventory_output:
        inventory_path = Path(inventory_output)
        if not inventory_path.is_absolute():
            inventory_path = (settings.root / inventory_path).resolve()
        inventory = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schemas": sorted({table.split(".", 1)[0] for table in selected}),
            "table_count": len(metadata),
            "column_count": sum(len(item["columns"]) for item in metadata),
            "tables": metadata,
        }
        write_json(inventory_path, inventory)

    unresolved_comments = sum(
        1
        for item in metadata
        for column in item["columns"]
        if not str(column.get("comment") or "").strip()
    )
    return {
        "status": "success",
        "tables": selected,
        "generated": report,
        "published": published,
        "index": index_result,
        "inventory": str(inventory_path) if inventory_path else None,
        "uncommented_columns": unresolved_comments,
        "note": (
            "数据库注释已生成描述并建立索引；占位说明可继续完善后运行catalog-sync --json，"
            "未核验关系不会生成JOIN。"
            if build_index
            else "数据库注释已生成描述；审阅后运行catalog-sync --json建立索引。"
        ),
    }


def ensure_catalog_ready(settings: Settings) -> dict:
    """业务MCP启动前检查索引；仅在显式配置时按固定范围自动自举。"""
    from dm_core.index import Snapshot

    auto = settings.boolean("MCP_AUTO_BOOTSTRAP", False)
    try:
        snapshot = Snapshot.load(settings)
        return {
            "action": "ready",
            "catalog_version": snapshot.catalog.version,
            "tables": len(snapshot.catalog.datasets),
        }
    except AppError as exc:
        if not auto or exc.code not in {"INDEX_INVALID", "INDEX_STALE", "CONFIG"}:
            raise

    tables = parse_table_list(settings.get("MCP_AUTO_TABLES"))
    schemas = _split_csv(settings.get("MCP_AUTO_SCHEMAS"))
    include = _split_csv(settings.get("MCP_AUTO_INCLUDE"))
    exclude = _split_csv(settings.get("MCP_AUTO_EXCLUDE"))
    if not tables and (not schemas or not include):
        raise AppError(
            "CONFIG",
            "自动建库需要MCP_AUTO_TABLES，或同时配置MCP_AUTO_SCHEMAS和MCP_AUTO_INCLUDE",
        )
    return bootstrap_catalog(
        settings,
        Database(settings),
        schemas=schemas or None,
        tables=tables or None,
        include=include or None,
        exclude=exclude or None,
        keep_existing=settings.boolean("MCP_AUTO_KEEP_EXISTING", False),
        max_tables=settings.integer("MCP_AUTO_MAX_TABLES", 50),
        build_index=True,
        inventory_output=settings.get("MCP_AUTO_INVENTORY_PATH") or None,
    )
