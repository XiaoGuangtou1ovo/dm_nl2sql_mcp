"""向量缓存、版本切换、索引失效与删除字段后的召回范围。"""

import pytest

from dm_core.catalog import build_catalog, description_path
from dm_core.config import AppError, Settings, read_json, write_json
from dm_core.index import Snapshot, sync


def test_only_changed_text_is_embedded_and_deleted_fields_are_removed(
    settings, metadata, embeddings
):
    catalog, source = build_catalog(settings, metadata)
    first = sync(settings, catalog, source, embeddings)
    assert first["embedded"] == len(catalog.documents())
    embeddings.calls.clear()
    assert sync(settings, catalog, source, embeddings)["action"] == "reused"
    assert not embeddings.calls
    path = description_path(settings, "S.EVENT")
    raw = read_json(path)
    raw["fields"]["NAME"] += "，另一种叫法为事件标题"
    write_json(path, raw)
    changed, source = build_catalog(settings, metadata)
    assert sync(settings, changed, source, embeddings)["embedded"] == 1
    assert len(embeddings.calls) == 1
    raw["fields"].pop("DEVICE_ID")
    write_json(path, raw)
    reduced, source = build_catalog(settings, metadata)
    assert sync(settings, reduced, source, embeddings)["embedded"] == 0
    snapshot = Snapshot.load(settings, embeddings)
    assert not any(r["id"].endswith(":DEVICE_ID") for r in snapshot.records)
    assert snapshot.search("事件", []) == []
    with pytest.raises(AppError):
        snapshot.search("事件", ["not_selected"])


def test_embedding_failure_keeps_current_version_and_changes_require_sync(
    settings, metadata, embeddings, monkeypatch
):
    catalog, source = build_catalog(settings, metadata)
    sync(settings, catalog, source, embeddings)
    pointer = settings.root / "artifacts/index/CURRENT"
    before = pointer.read_bytes()
    path = description_path(settings, "S.EVENT")
    raw = read_json(path)
    raw["fields"]["NAME"] = "新的业务解释"
    write_json(path, raw)
    with pytest.raises(AppError) as exc:
        Snapshot.load(settings, embeddings)
    assert exc.value.code == "INDEX_STALE"
    changed, source = build_catalog(settings, metadata)

    def fail(_):
        raise AppError("EMBEDDING_UNAVAILABLE", "测试中断")

    monkeypatch.setattr(embeddings, "embed_documents", fail)
    with pytest.raises(AppError):
        sync(settings, changed, source, embeddings)
    assert pointer.read_bytes() == before


def test_model_change_rejects_same_dimension_old_vectors(settings, catalog, embeddings):
    c, source = catalog
    sync(settings, c, source, embeddings)
    changed = Settings(
        settings.root, {**settings.values, "EMBEDDING_MODEL": "new-model"}, settings.env_file
    )
    with pytest.raises(AppError) as exc:
        Snapshot.load(changed)
    assert exc.value.code == "INDEX_STALE"
