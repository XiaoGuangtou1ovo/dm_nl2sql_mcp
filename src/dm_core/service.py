"""完整调用链：检索 → 规划 → 编译 → 校验 → 实库结构核验及执行 → 审计。"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from dm_core.config import AppError, Settings
from dm_core.database import Database
from dm_core.index import Snapshot
from dm_core.planner import ApiPlanner, Planner
from dm_core.sql import Compiler, validate_sql

logger = logging.getLogger(__name__)


class QueryService:
    """独立可复用服务，无会话全局 Schema；测试时可注入检索器和规划器。"""

    def __init__(
        self,
        settings: Settings,
        *,
        planner: Planner | None = None,
        retriever=None,
    ):
        self.s = settings
        self.retriever = retriever or Snapshot.load(settings)
        self.catalog = self.retriever.catalog
        self.planner = planner or ApiPlanner(settings)
        self.compiler = Compiler(self.catalog, settings.integer("MAX_ROWS", 100))
        self.database = Database(settings)
        self.semaphore = threading.BoundedSemaphore(settings.integer("MAX_CONCURRENT_QUERIES", 2))
        self.audit_lock = threading.Lock()

    def list_datasets(self) -> dict:
        return {
            "catalog_version": self.catalog.version,
            "datasets": [
                {
                    "id": d.id,
                    "name": d.name,
                    "description": d.description,
                    "entity_id": d.entity_id,
                }
                for d in self.catalog.datasets.values()
            ],
        }

    def search_catalog(self, question: str, limit: int = 5) -> dict:
        """只检索业务目录，不访问数据库；供智能体先定位表再调用query_data。"""
        if not question.strip() or len(question) > 4000:
            return {
                "status": "error",
                "error_code": "INPUT_INVALID",
                "message": "问题长度必须在1～4000个字符之间",
            }
        if not 1 <= limit <= 20:
            return {
                "status": "error",
                "error_code": "INPUT_INVALID",
                "message": "limit必须在1～20之间",
            }
        try:
            candidates = []
            for hit in self.retriever.search(question, k=limit):
                dataset = self.catalog.get(hit["dataset_id"])
                matched = hit["matched_record"].split(":")
                matched_kind = matched[-2] if len(matched) >= 3 else "table"
                matched_name = matched[-1] if matched_kind != "table" else dataset.name
                candidates.append(
                    {
                        "dataset_id": dataset.id,
                        "name": dataset.name,
                        "description": dataset.description,
                        "physical_table": (
                            f"{dataset.physical.schema_name}.{dataset.physical.table}"
                        ),
                        "entity_id": dataset.entity_id,
                        "score": round(hit["score"], 6),
                        "matched_kind": matched_kind,
                        "matched_name": matched_name,
                        "evidence": hit["matched_text"],
                    }
                )
            return {
                "status": "success",
                "catalog_version": self.catalog.version,
                "candidates": candidates,
                "note": "可将candidates中的dataset_id传给query_data以缩小检索范围。",
            }
        except AppError as exc:
            return {
                "status": "denied" if exc.denied else "error",
                "error_code": exc.code,
                "message": str(exc),
            }

    def query(
        self,
        question: str,
        dataset_ids: list[str] | None = None,
        max_rows: int = 100,
        *,
        now: datetime | None = None,
    ) -> dict:
        """查询入口；把业务问题转为受限计划并执行，返回统一状态而非向外抛驱动异常。

        dataset_ids 只缩小环境允许范围；now 仅供确定性评测注入，MCP 不暴露该参数。
        在 finally 中统一记录审计，因此澄清、拒绝和失败也能用 query_id 追踪。
        """
        query_id, started = uuid.uuid4().hex, time.monotonic()
        instant = now or datetime.now(timezone.utc)
        result = {
            "query_id": query_id,
            "status": "error",
            "data": [],
            "columns": [],
            "returned_rows": 0,
            "truncated": False,
            "time_range": None,
            "sources": [],
            "warnings": [],
            "queried_at": instant.isoformat(),
        }
        audit = {
            "query_id": query_id,
            "queried_at": instant.isoformat(),
            "catalog_hash": self.catalog.hash,
        }
        acquired = self.semaphore.acquire(blocking=False)
        try:
            if not acquired:
                raise AppError("BUSY", "服务忙，请稍后重试")
            if not question.strip() or len(question) > 4000:
                raise AppError("INPUT_INVALID", "问题长度必须在 1～4000 个字符之间")
            if max_rows < 1:
                raise AppError("INPUT_INVALID", "max_rows 必须为正数")
            logger.info("query_id=%s stage=retrieval", query_id)
            candidates = self.retriever.search(question, dataset_ids)
            ids = [c["dataset_id"] for c in candidates]
            if not ids:
                result.update(
                    status="needs_clarification", message="没有候选数据集，请选择可用业务数据。"
                )
                return result
            # 仅补充允许目录中的直接关联，不把整个库发送给模型。
            for key in list(ids):
                for relation in self.catalog.get(key).relations:
                    target = relation.target_dataset_id
                    if target in self.catalog.datasets and target not in ids:
                        if dataset_ids is None or target in dataset_ids:
                            ids.append(target)
            context = self.catalog.context(ids)
            audit["candidates"] = ids
            # 只修正一次计划错误；权限、网络和数据库错误不进入此循环。
            previous_error = None
            for attempt in range(2):
                try:
                    logger.info("query_id=%s stage=planning attempt=%d", query_id, attempt + 1)
                    plan = self.planner.plan(question, context, instant, previous_error)
                    if plan.status == "needs_clarification":
                        result.update(
                            status="needs_clarification",
                            message=plan.clarification or "请补充业务范围。",
                        )
                        return result
                    plan.limit = min(plan.limit, max_rows)
                    compiled = self.compiler.compile(plan, ids, instant)
                    validate_sql(compiled, self.catalog)
                    break
                except AppError as exc:
                    if exc.code != "PLAN_INVALID" or exc.denied or attempt == 1:
                        raise
                    previous_error = str(exc)
            logger.info("query_id=%s stage=database", query_id)
            result.update(
                self.database.execute(
                    compiled, [self.catalog.get(key) for key in compiled.dataset_ids]
                )
            )
            result.update(
                status="success",
                time_range=compiled.time_range,
                sources=[
                    {"dataset_id": key, "catalog_version": self.catalog.version}
                    for key in compiled.dataset_ids
                ],
                message="查询成功" if result["data"] else "查询成功，没有符合条件的记录",
            )
            if result["truncated"]:
                result["warnings"].append("结果达到行数或体积上限，当前返回不是完整明细。")
                if not result["data"]:
                    result["message"] = "查询成功，但记录超过返回体积上限；请减少输出字段。"
            audit["sql"] = compiled.sql
            # 不保存原始问题、参数和行数据，减少审计日志中的敏感信息。
            audit["plan_attempts"] = attempt + 1
        except AppError as exc:
            result.update(
                status="denied" if exc.denied else "error", error_code=exc.code, message=str(exc)
            )
        except Exception:
            result.update(
                status="error", error_code="INTERNAL", message="内部处理失败，请使用查询 ID 排查"
            )
        finally:
            if acquired:
                self.semaphore.release()
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            audit.update(
                status=result["status"],
                error_code=result.get("error_code"),
                elapsed_ms=result["elapsed_ms"],
            )
            try:
                path = self.s.path("AUDIT_PATH", "artifacts/audit.jsonl")
                with self.audit_lock:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as file:
                        file.write(json.dumps(audit, ensure_ascii=False) + "\n")
            except OSError:
                result["warnings"].append("审计记录写入失败，请检查日志目录权限。")
        return result
