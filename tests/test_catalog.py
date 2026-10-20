"""关注用户维护流程：保留描述、自动取结构、拒绝错误字段与未经确认的关联。"""

import sqlite3
from datetime import datetime, timezone

import pytest

from dm_core.catalog import build_catalog, dataset_id, description_path, initialize, selected_tables
from dm_core.config import AppError, read_json, write_json
from dm_core.models import Filter, QueryPlan, TimeRange
from dm_core.sql import Compiler, validate_sql


def test_initialize_preserves_manual_text_and_does_not_expose_new_columns(settings, metadata):
    path = description_path(settings, "S.EVENT")
    before = path.read_bytes()
    metadata[0]["columns"].append(
        {"name": "NEW_COL", "type": "NUMBER", "nullable": True, "comment": "新列"}
    )
    result = initialize(settings, metadata)
    assert path.read_bytes() == before
    assert result["changes"][0]["not_described"] == ["NEW_COL"]
    path.unlink()
    metadata[0]["columns"].append(
        {"name": "PAYLOAD", "type": "CLOB", "nullable": True, "comment": "大文本"}
    )
    initialize(settings, metadata)
    raw = read_json(path)
    assert "NEW_COL" in raw["fields"]
    assert "PAYLOAD" not in raw["fields"]


def test_physical_keys_and_types_are_automatic_and_no_pk_table_is_queryable(catalog):
    c, _ = catalog
    event = c.get(dataset_id("S.EVENT"))
    assert event.primary_key == []
    assert c.get(dataset_id("S.DEVICE")).primary_key == ["ID"]
    malicious = "' OR 1=1; DROP TABLE EVENT --"
    plan = QueryPlan(
        dataset_id=event.id,
        fields=[f"{event.id}.NAME"],
        filters=[Filter(field=f"{event.id}.NAME", op="eq", value=malicious)],
    )
    query = Compiler(c, 1).compile(plan, list(c.datasets), datetime.now(timezone.utc))
    validate_sql(query, c)
    assert malicious not in query.sql
    assert query.params == [malicious]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("ATTACH DATABASE ':memory:' AS S")
        conn.execute("CREATE TABLE S.EVENT(NAME TEXT)")
        conn.execute("INSERT INTO S.EVENT VALUES(?)", (malicious,))
        assert conn.execute(query.sql, query.params).fetchall() == [(malicious,)]


def test_missing_field_reports_exact_name_and_default_time_must_be_date(settings, metadata):
    metadata[0]["columns"] = metadata[0]["columns"][1:]
    with pytest.raises(AppError, match="S.EVENT.NAME"):
        build_catalog(settings, metadata)
    raw = read_json(description_path(settings, "S.EVENT"))
    raw["fields"].pop("NAME")
    raw["default_time"] = "DEVICE_ID"
    write_json(description_path(settings, "S.EVENT"), raw)
    with pytest.raises(AppError, match="default_time"):
        build_catalog(settings, metadata)


def test_explicit_relation_requires_target_real_primary_key(settings, metadata):
    path = description_path(settings, "S.EVENT")
    raw = read_json(path)
    raw["relations"] = [
        {"target": "S.DEVICE", "on": {"DEVICE_ID": "ID"}, "description": "事件所属设备"}
    ]
    write_json(path, raw)
    catalog, _ = build_catalog(settings, metadata)
    event = catalog.get(dataset_id("S.EVENT"))
    device = catalog.get(dataset_id("S.DEVICE"))
    plan = QueryPlan(
        dataset_id=event.id,
        fields=[f"{device.id}.NAME"],
        time_range=TimeRange(mode="recent_days", days=3),
    )
    query = Compiler(catalog, 10).compile(
        plan, list(catalog.datasets), datetime(2026, 10, 16, tzinfo=timezone.utc)
    )
    validate_sql(query, catalog)
    assert "LEFT JOIN" in query.sql
    assert len(query.params) == 2
    metadata[1]["primary_key"] = []
    with pytest.raises(AppError, match="主键"):
        build_catalog(settings, metadata)


def test_invalid_table_selection_and_numeric_metric_are_rejected(settings, metadata):
    write_json(settings.root / "configs/tables.json", {"tables": ["S.EVENT;DROP TABLE X"]})
    with pytest.raises((AppError, ValueError)):
        selected_tables(settings)
    write_json(settings.root / "configs/tables.json", {"tables": ["S.EVENT", "S.DEVICE"]})
    path = description_path(settings, "S.EVENT")
    raw = read_json(path)
    raw["metrics"] = [
        {
            "id": "sum_name",
            "name": "错误指标",
            "description": "不能对字符串求和",
            "aggregation": "sum",
            "field": "NAME",
        }
    ]
    write_json(path, raw)
    with pytest.raises(AppError, match="数值"):
        build_catalog(settings, metadata)
