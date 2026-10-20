"""查询状态、元数据漂移、真实stdio MCP及LangChain桥接。"""

import asyncio
import json
import os
import sqlite3
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dm_core.bootstrap import ensure_catalog_ready
from dm_core.catalog import dataset_id
from dm_core.config import AppError
from dm_core.database import verify_dataset
from dm_core.index import Snapshot, sync
from dm_core.models import QueryPlan
from dm_core.qa import connect, load_tools
from dm_core.service import QueryService


def test_query_result_empty_error_and_scope_are_distinct(
    settings, catalog, embeddings, monkeypatch
):
    c, source = catalog
    sync(settings, c, source, embeddings)
    key = dataset_id("S.EVENT")

    class Planner:
        def plan(self, *args):
            return QueryPlan(dataset_id=key, fields=[f"{key}.NAME"], limit=1)

    service = QueryService(
        settings, planner=Planner(), retriever=Snapshot.load(settings, embeddings)
    )

    def execute(query, _):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("ATTACH DATABASE ':memory:' AS S")
            conn.execute("CREATE TABLE S.EVENT(NAME TEXT)")
            conn.execute("INSERT INTO S.EVENT VALUES('合成事件')")
            rows = conn.execute(query.sql, query.params).fetchall()
        return {
            "data": [{"event_name": row[0]} for row in rows],
            "columns": [],
            "returned_rows": len(rows),
            "truncated": False,
        }

    monkeypatch.setattr(service.database, "execute", execute)
    result = service.query("查看一条事件")
    assert result["status"] == "success" and result["returned_rows"] == 1
    assert service.query("事件", dataset_ids=[])["status"] == "needs_clarification"
    assert service.query("事件", dataset_ids=["secret"])["status"] == "denied"

    def reject(*_):
        raise AppError("SCHEMA_CHANGED", "类型变化")

    monkeypatch.setattr(service.database, "execute", reject)
    assert service.query("事件")["error_code"] == "SCHEMA_CHANGED"
    log = (settings.root / "artifacts/audit.jsonl").read_text(encoding="utf-8")
    assert "合成事件" not in log and "查看一条事件" not in log


def test_runtime_schema_drift_blocks_execution(catalog, metadata):
    c, _ = catalog
    metadata[0]["columns"][0]["type"] = "NUMBER"
    with pytest.raises(AppError) as exc:
        verify_dataset(c.get(dataset_id("S.EVENT")), metadata[0])
    assert exc.value.code == "SCHEMA_CHANGED"


def test_catalog_search_exposes_table_evidence_without_database_access(settings, catalog):
    c, _ = catalog
    key = dataset_id("S.EVENT")

    class Retriever:
        catalog = c

        def search(self, question, allowed_ids=None, k=3):
            assert question == "事件"
            return [
                {
                    "dataset_id": key,
                    "score": 1.25,
                    "matched_record": f"{key}:field:NAME",
                    "matched_text": "S.EVENT 事件 NAME 事件名称",
                }
            ]

    service = QueryService(settings, retriever=Retriever(), planner=object())
    result = service.search_catalog("事件", limit=2)
    assert result["status"] == "success"
    assert result["candidates"][0]["matched_kind"] == "field"
    assert result["candidates"][0]["matched_name"] == "NAME"
    assert result["candidates"][0]["physical_table"] == "S.EVENT"


def test_auto_bootstrap_uses_only_configured_scope(settings, monkeypatch):
    settings.values.update(
        {
            "MCP_AUTO_BOOTSTRAP": "true",
            "MCP_AUTO_TABLES": "S.EVENT,S.DEVICE",
            "MCP_AUTO_SCHEMAS": "",
            "MCP_AUTO_INCLUDE": "",
            "MCP_AUTO_EXCLUDE": "*_LOG",
            "MCP_AUTO_MAX_TABLES": "10",
        }
    )

    def stale(_cls, _settings):
        raise AppError("INDEX_INVALID", "索引不存在")

    monkeypatch.setattr(Snapshot, "load", classmethod(stale))
    captured = {}

    def fake_bootstrap(_settings, _database, **kwargs):
        captured.update(kwargs)
        return {"action": "built", "tables": kwargs["tables"]}

    monkeypatch.setattr("dm_core.bootstrap.bootstrap_catalog", fake_bootstrap)
    result = ensure_catalog_ready(settings)
    assert result["action"] == "built"
    assert captured["tables"] == ["S.EVENT", "S.DEVICE"]
    assert captured["exclude"] == ["*_LOG"]
    assert captured["build_index"] is True

    settings.values["MCP_AUTO_TABLES"] = ""
    with pytest.raises(AppError) as exc:
        ensure_catalog_ready(settings)
    assert exc.value.code == "CONFIG"


def test_real_stdio_mcp_and_langchain_tool_bridge(settings, catalog, embeddings):
    c, source = catalog
    sync(settings, c, source, embeddings)

    async def check():
        async with connect(settings) as session:
            descriptors = await session.list_tools()
            assert {t.name for t in descriptors.tools} == {
                "list_datasets",
                "search_catalog",
                "query_data",
            }
            tools = await load_tools(session)
            listing = next(t for t in tools if t.name == "list_datasets")
            payload = json.loads(await listing.ainvoke({}))
            assert len(payload["datasets"]) == 2

    asyncio.run(check())


def test_admin_mcp_exposes_onboarding_tools(settings, catalog, embeddings):
    c, source = catalog
    sync(settings, c, source, embeddings)

    async def check():
        env = dict(os.environ)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dm_core.admin_mcp", "--env-file", str(settings.env_file)],
            cwd=settings.root,
            env=env,
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                descriptors = await session.list_tools()
                assert {tool.name for tool in descriptors.tools} == {
                    "get_catalog_status",
                    "discover_schema",
                    "inspect_table",
                    "bootstrap_catalog",
                    "refresh_catalog",
                }
                status = await session.call_tool("get_catalog_status", {})
                assert status.structuredContent["status"] == "ready"

    asyncio.run(check())
