"""Embedding 统一适配：兼容 API/vLLM 复用协议，Ollama 使用 /api/embed。"""

import time
from typing import Protocol

import httpx
import numpy as np

from dm_core.config import AppError, Settings


class Embeddings(Protocol):
    """检索层依赖此接口；模型接入不依赖 FAISS 或未来的 Milvus。"""

    @property
    def fingerprint(self) -> dict: ...
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class EmbeddingClient:
    """同步批量请求；由 MCP 异步边界转在线程运行，重试仅限临时错误。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.provider = settings.required("EMBEDDING_PROVIDER")
        if self.provider not in {"openai_compatible", "vllm", "ollama"}:
            raise AppError("CONFIG", "不支持的 EMBEDDING_PROVIDER")
        self.model = settings.required("EMBEDDING_MODEL")
        self.base = settings.get("EMBEDDING_BASE_URL").rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            raise AppError("CONFIG", "EMBEDDING_BASE_URL 必须为 HTTP 服务根地址")
        self.transport = transport
        self.normalize = settings.boolean("EMBEDDING_NORMALIZE")

    @property
    def fingerprint(self) -> dict:
        """不含 Key；地址变化也要求重新构建，防止别名相同但部署模型不同。"""
        return {
            "provider": self.provider,
            "base_url": self.base,
            "model": self.model,
            "revision": self.s.get("EMBEDDING_MODEL_REVISION"),
            "normalize": self.normalize,
            "expected_dimension": self.s.get("EMBEDDING_EXPECTED_DIMENSION"),
            "query_prefix": self.s.get("EMBEDDING_QUERY_PREFIX"),
            "document_prefix": self.s.get("EMBEDDING_DOCUMENT_PREFIX"),
            "text_builder_version": 1,
        }

    def _request(self, texts: list[str]) -> list[list[float]]:
        key = self.s.get("EMBEDDING_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if self.provider == "ollama":
            url = self.base + "/api/embed"
            body = {"model": self.model, "input": texts, "truncate": False}
        else:
            url = self.base + "/embeddings"
            body = {"model": self.model, "input": texts, "encoding_format": "float"}
        retries = self.s.integer("EMBEDDING_MAX_RETRIES", 2, minimum=0)
        timeout = self.s.integer("EMBEDDING_TIMEOUT_SECONDS", 60)
        with httpx.Client(timeout=timeout, transport=self.transport) as client:
            for attempt in range(retries + 1):
                try:
                    response = client.post(url, json=body, headers=headers)
                    if response.status_code in {429, 502, 503, 504} and attempt < retries:
                        time.sleep(min(2**attempt, 4))
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if self.provider == "ollama":
                        return data["embeddings"]
                    records = sorted(data["data"], key=lambda item: item["index"])
                    if [r["index"] for r in records] != list(range(len(texts))):
                        raise ValueError("响应索引不连续")
                    return [r["embedding"] for r in records]
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt == retries:
                        raise AppError(
                            "EMBEDDING_UNAVAILABLE", "Embedding 服务网络错误或超时"
                        ) from exc
                except (httpx.HTTPStatusError, ValueError, KeyError, TypeError) as exc:
                    raise AppError(
                        "EMBEDDING_INVALID", "Embedding 请求失败或响应无效，请检查服务与模型"
                    ) from exc
        raise AppError("EMBEDDING_UNAVAILABLE", "Embedding 请求未完成")

    def _embed(self, texts: list[str], prefix: str) -> np.ndarray:
        if not texts or any(not text.strip() for text in texts):
            raise AppError("EMBEDDING_INPUT", "Embedding 输入不能为空")
        size = self.s.integer("EMBEDDING_BATCH_SIZE", 16)
        batches = []
        for start in range(0, len(texts), size):
            chunk = [prefix + text for text in texts[start : start + size]]
            try:
                vectors = np.asarray(self._request(chunk), dtype="float32")
            except (ValueError, TypeError) as exc:
                raise AppError("EMBEDDING_INVALID", "Embedding 向量格式错误") from exc
            if vectors.ndim != 2 or vectors.shape[0] != len(chunk) or not vectors.shape[1]:
                raise AppError("EMBEDDING_INVALID", "Embedding 向量数量或维度错误")
            if not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) == 0).any():
                raise AppError("EMBEDDING_INVALID", "Embedding 包含非有限数值或零向量")
            expected = self.s.get("EMBEDDING_EXPECTED_DIMENSION")
            if expected and vectors.shape[1] != self.s.integer("EMBEDDING_EXPECTED_DIMENSION", 1):
                raise AppError("EMBEDDING_DIMENSION", "Embedding 实际维度与配置不同")
            if batches and batches[0].shape[1] != vectors.shape[1]:
                raise AppError("EMBEDDING_DIMENSION", "Embedding 批次之间维度不一致")
            if self.normalize:
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            batches.append(vectors)
        return np.ascontiguousarray(np.concatenate(batches), dtype="float32")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, self.s.get("EMBEDDING_DOCUMENT_PREFIX"))

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], self.s.get("EMBEDDING_QUERY_PREFIX"))
