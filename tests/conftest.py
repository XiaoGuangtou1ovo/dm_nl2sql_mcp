"""合成元数据与描述测试，不读取本机.env或真实业务数据。"""

import hashlib

import numpy as np
import pytest

from dm_core.catalog import build_catalog, description_path
from dm_core.config import Settings, write_json
from dm_core.embeddings import EmbeddingClient


class Embeddings:
    """仅测试用的确定性向量，生产代码没有演示模型或隐式回退。"""

    def __init__(self, settings):
        self.fingerprint = EmbeddingClient(settings).fingerprint
        self.calls = []

    def embed_documents(self, texts):
        self.calls.extend(texts)
        vectors = np.array(
            [list(hashlib.sha256(t.encode()).digest()) for t in texts], dtype="float32"
        )
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def embed_query(self, text):
        return self.embed_documents([text])


@pytest.fixture
def settings(tmp_path):
    values = {
        "DAMENG_HOST": "127.0.0.1",
        "DAMENG_PORT": "5236",
        "DAMENG_USER": "test_reader",
        "EMBEDDING_PROVIDER": "openai_compatible",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
        "EMBEDDING_MODEL": "test-model",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_MODEL": "test-chat",
        "MCP_ADMIN_TOKEN": "test-admin-token",
    }
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    s = Settings(tmp_path, values, path)
    write_json(tmp_path / "configs/tables.json", {"tables": ["S.EVENT", "S.DEVICE"]})
    write_json(
        description_path(s, "S.EVENT"),
        {
            "name": "事件",
            "description": "业务事件记录",
            "fields": {"NAME": "事件名称", "AT": "发生时间", "DEVICE_ID": "设备编号"},
            "default_time": "AT",
        },
    )
    write_json(
        description_path(s, "S.DEVICE"),
        {
            "name": "设备",
            "description": "设备主档",
            "fields": {"ID": "设备编号", "NAME": "设备名称"},
        },
    )
    return s


@pytest.fixture
def metadata():
    def c(name, kind, nullable=True):
        return {"name": name, "type": kind, "nullable": nullable, "comment": ""}

    return [
        {
            "schema": "S",
            "table": "EVENT",
            "primary_key": [],
            "columns": [c("NAME", "VARCHAR2"), c("AT", "TIMESTAMP"), c("DEVICE_ID", "NUMBER")],
        },
        {
            "schema": "S",
            "table": "DEVICE",
            "primary_key": ["ID"],
            "columns": [c("ID", "NUMBER", False), c("NAME", "VARCHAR2")],
        },
    ]


@pytest.fixture
def catalog(settings, metadata):
    return build_catalog(settings, metadata)


@pytest.fixture
def embeddings(settings):
    return Embeddings(settings)
