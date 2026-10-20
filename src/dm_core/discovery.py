"""首次接库的选表与清单生成：只访问元数据，不读取业务行。"""

import fnmatch
import re

from dm_core.catalog import split_table
from dm_core.config import AppError


def parse_table_list(value: str | None) -> list[str]:
    if not value:
        return []
    tables = [item.strip() for item in re.split(r"[\n,，;；]+", value) if item.strip()]
    for table in tables:
        split_table(table)
    if len(tables) != len(set(tables)):
        raise AppError("CONFIG", "选表清单存在重复表名")
    return tables


def filter_table_refs(
    table_refs: list[str],
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    max_tables: int,
) -> list[str]:
    """通配符同时匹配完整表和裸表名；结果按物理名称排序。"""
    if max_tables < 1:
        raise AppError("CONFIG", "max_tables必须为正数")
    includes = [pattern.strip() for pattern in include or ["*"] if pattern.strip()]
    excludes = [pattern.strip() for pattern in exclude or [] if pattern.strip()]
    if not includes:
        includes = ["*"]

    def matches(ref: str, pattern: str) -> bool:
        schema, table = split_table(ref)
        target = ref if "." in pattern else table
        return fnmatch.fnmatchcase(target.casefold(), pattern.casefold()) or fnmatch.fnmatchcase(
            schema.casefold(), pattern.casefold()
        )

    selected = []
    for ref in sorted(set(table_refs)):
        split_table(ref)
        if any(matches(ref, pattern) for pattern in includes) and not any(
            matches(ref, pattern) for pattern in excludes
        ):
            selected.append(ref)
    if not selected:
        raise AppError("TABLE_SCOPE_EMPTY", "选表条件没有匹配到任何可见表")
    if len(selected) > max_tables:
        raise AppError(
            "TABLE_SCOPE_TOO_LARGE",
            f"选表结果有{len(selected)}张表，超过上限{max_tables}；请缩小通配符或提高--max-tables",
        )
    return selected


def table_refs(rows: list[dict]) -> list[str]:
    return sorted({f"{row['schema']}.{row['table']}" for row in rows})
