"""描述向量缓存与FAISS：仅重算变化文本，目录/向量/索引以同一版本原子发布。"""

import os
import uuid

import faiss
import numpy as np

from dm_core.catalog import Catalog, source_material
from dm_core.config import AppError, digest, read_json, write_json
from dm_core.embeddings import EmbeddingClient


def current_directory(root):
    version = (root / "CURRENT").read_text(encoding="ascii").strip()
    if len(version) != 32 or any(c not in "0123456789abcdef" for c in version):
        raise ValueError("索引指针无效")
    return root / version


def sync(settings, catalog, source_hash, embeddings=None):
    """缓存按文本与模型指纹复用；FAISS本身从复用/新增向量重建，避免残留已删除字段。"""
    embeddings = embeddings or EmbeddingClient(settings)
    root = settings.path("INDEX_DIR", "artifacts/index")
    records = catalog.documents()
    keys = [digest(r["text"]) for r in records]
    cache = {}
    previous = None
    try:
        directory = current_directory(root)
        previous = read_json(directory / "manifest.json")
        if previous["embedding"] == embeddings.fingerprint:
            vectors = np.load(directory / "vectors.npy", allow_pickle=False)
            index_bytes = np.frombuffer(
                (directory / "index.faiss").read_bytes(), dtype="uint8"
            ).copy()
            old_index = faiss.deserialize_index(index_bytes)
            if (
                vectors.shape != (len(previous["records"]), previous["dimension"])
                or old_index.ntotal != len(vectors)
                or old_index.d != vectors.shape[1]
                or not np.isfinite(vectors).all()
                or (np.linalg.norm(vectors, axis=1) == 0).any()
            ):
                raise ValueError("向量缓存损坏")
            cache = {
                digest(r["text"]): v for r, v in zip(previous["records"], vectors, strict=True)
            }
            if (
                previous["catalog_hash"] == catalog.hash
                and previous["source_hash"] == source_hash
                and digest(read_json(directory / "catalog.json")) == catalog.hash
            ):
                return {
                    "action": "reused",
                    "tables": len(catalog.datasets),
                    "documents": len(records),
                    "embedded": 0,
                    "dimension": vectors.shape[1],
                }
    except (OSError, ValueError, KeyError, RuntimeError, AppError):
        cache = {}
    missing = list(dict.fromkeys(key for key in keys if key not in cache))
    texts = {key: r["text"] for key, r in zip(keys, records, strict=True)}
    if missing:
        vectors = embeddings.embed_documents([texts[key] for key in missing])
        if cache and any(len(v) != vectors.shape[1] for v in cache.values()):
            raise AppError(
                "EMBEDDING_DIMENSION",
                "同名模型维度已变化，请更新EMBEDDING_MODEL_REVISION后重新同步",
            )
        cache.update(zip(missing, vectors, strict=True))
    matrix = np.asarray([cache[key] for key in keys], dtype="float32")
    faiss.omp_set_num_threads(1)
    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(matrix)
    version = uuid.uuid4().hex
    directory = root / version
    directory.mkdir(parents=True)
    manifest = {
        "source_hash": source_hash,
        "catalog_hash": catalog.hash,
        "embedding": embeddings.fingerprint,
        "dimension": matrix.shape[1],
        "records": records,
    }
    # 写完所有文件才切换指针，网络或写盘失败不会破坏当前可用版本。
    write_json(directory / "catalog.json", catalog.raw)
    write_json(directory / "manifest.json", manifest)
    np.save(directory / "vectors.npy", matrix, allow_pickle=False)
    (directory / "index.faiss").write_bytes(faiss.serialize_index(index).tobytes())
    temp = root / f"CURRENT.{version}.tmp"
    temp.write_text(version, encoding="ascii")
    os.replace(temp, root / "CURRENT")
    return {
        "action": "built",
        "tables": len(catalog.datasets),
        "documents": len(records),
        "embedded": len(missing),
        "reused": len(records) - sum(key in missing for key in keys),
        "dimension": matrix.shape[1],
    }


class Snapshot:
    def __init__(self, catalog, index, records, embeddings):
        self.catalog, self.index, self.records, self.embeddings = (
            catalog,
            index,
            records,
            embeddings,
        )

    @classmethod
    def load(cls, settings, embeddings=None):
        embeddings = embeddings or EmbeddingClient(settings)
        _, _, source_hash = source_material(settings)
        try:
            directory = current_directory(settings.path("INDEX_DIR", "artifacts/index"))
            manifest = read_json(directory / "manifest.json")
            if (
                manifest["source_hash"] != source_hash
                or manifest["embedding"] != embeddings.fingerprint
            ):
                raise AppError(
                    "INDEX_STALE", "描述、选表、连接或模型配置已变化，请catalog-sync后重启问答"
                )
            catalog = Catalog(read_json(directory / "catalog.json"))
            if catalog.hash != manifest["catalog_hash"]:
                raise ValueError("目录指纹不一致")
            index = faiss.deserialize_index(
                np.frombuffer((directory / "index.faiss").read_bytes(), dtype="uint8").copy()
            )
            records = manifest["records"]
            if index.ntotal != len(records) or index.d != manifest["dimension"]:
                raise ValueError("索引与映射不一致")
            if records != catalog.documents():
                raise ValueError("检索记录与目录不一致")
            return cls(catalog, index, records, embeddings)
        except AppError as exc:
            if exc.code == "INDEX_STALE":
                raise
            raise AppError("INDEX_INVALID", "索引包不可用，请catalog-sync") from exc
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise AppError("INDEX_INVALID", "索引不存在或损坏，请先catalog-sync") from exc

    def search(self, question, allowed_ids=None, k=3):
        allowed = set(allowed_ids) if allowed_ids is not None else set(self.catalog.datasets)
        for key in allowed:
            self.catalog.get(key)
        if not allowed:
            return []
        vector = self.embeddings.embed_query(question)
        if vector.shape[1] != self.index.d:
            raise AppError("INDEX_STALE", "查询向量维度变化，请更新模型版本后catalog-sync")
        faiss.omp_set_num_threads(1)
        scores, positions = self.index.search(vector, self.index.ntotal)
        results = {}
        for score, position in zip(scores[0], positions[0], strict=True):
            record = self.records[int(position)]
            key = record["dataset_id"]
            if key not in allowed:
                continue
            d = self.catalog.get(key)
            terms = [d.name, d.physical.table, *d.aliases]
            for entity_id in {field.entity_id for field in d.fields if field.entity_id}:
                entity = self.catalog.entities.get(entity_id)
                if entity:
                    terms.extend([entity.name, *entity.aliases])
            exact = any(t and t.casefold() in question.casefold() for t in terms)
            field_match = any(
                f.business_name.casefold() in question.casefold()
                or f.name.casefold() in question.casefold()
                or f.property_id
                and f.property_id.casefold() in question.casefold()
                or any(term.casefold() in question.casefold() for term in f.synonyms)
                for f in d.fields
            )
            score = float(score) + (1 if exact else 0) + (0.2 if field_match else 0)
            if key not in results or results[key]["score"] < score:
                results[key] = {
                    "dataset_id": key,
                    "score": score,
                    "catalog_version": self.catalog.version,
                    "matched_record": record["id"],
                    "matched_text": record["text"][:320],
                }
        return sorted(results.values(), key=lambda r: (-r["score"], r["dataset_id"]))[:k]
