"""共享数据契约：模型只能填写受限计划，不能提供任意 SQL/函数/表达式。"""

import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """拒绝未知字段，避免模型生成的多余指令穿过协议边界。"""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)


class Filter(StrictModel):
    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "is_null", "not_null"]
    value: str | int | float | bool | list[str | int | float | bool] | None = None


class TimeRange(StrictModel):
    """服务端计算相对时间；range 的 start/end 必须携带时区或为 YYYY-MM-DD。"""

    field: str | None = None
    mode: Literal["recent_days", "today", "yesterday", "this_week", "last_week", "range"]
    days: int | None = Field(default=None, ge=1, le=3660)
    start: str | None = None
    end: str | None = None


class Order(StrictModel):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(StrictModel):
    """列引用格式 dataset_id.FIELD，指标格式 dataset_id.metric_id。"""

    status: Literal["ready", "needs_clarification"] = "ready"
    clarification: str | None = None
    dataset_id: str | None = None
    fields: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    time_range: TimeRange | None = None
    order_by: list[Order] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=100, ge=1, le=10000)


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def identifier(value: str) -> str:
    """首版限定 ASCII 标识符；不允许把 SQL 片段藏在目录名称中。"""
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"不支持的标识符：{value}")
    return value


class Physical(StrictModel):
    datasource_id: str
    schema_name: str = Field(alias="schema", description="达梦 Schema 名称，保留物理大小写")
    table: str

    _names = field_validator("datasource_id", "schema_name", "table")(identifier)


class EnumValue(StrictModel):
    value: str | int | float | bool
    label: str


SemanticType = Literal[
    "identifier",
    "name",
    "code",
    "text",
    "boolean",
    "integer",
    "number",
    "quantity",
    "date",
    "datetime",
    "enum",
    "other",
]
SemanticRole = Literal[
    "identifier",
    "planned_time",
    "actual_time",
    "ingestion_time",
    "dimension",
    "measure",
    "descriptive",
]


class Column(StrictModel):
    name: str
    physical_type: str
    business_name: str
    description: str
    role: Literal["event_time", "ingestion_time", "dimension", "measure"] | None = None
    entity_id: str | None = None
    property_id: str | None = None
    semantic_type: SemanticType | None = None
    semantic_role: SemanticRole | None = None
    synonyms: list[str] = Field(default_factory=list)
    nullable: bool = True
    enum: list[EnumValue] = Field(default_factory=list)
    unit: str | None = None
    null_meaning: str | None = None

    _name = field_validator("name")(identifier)
    _ontology_names = field_validator("entity_id", "property_id")(
        lambda value: identifier(value) if value else value
    )


class Entity(StrictModel):
    """业务实体独立于物理表；一个实体可由多个数据集的字段共同描述。"""

    id: str
    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    grain: str | None = None
    status: Literal["verified", "pending", "conflict"] = "pending"

    _id = field_validator("id")(identifier)


class RelationPath(StrictModel):
    """语义关系的一条已核验物理路径，仅指向目标真实主键。"""

    source_dataset_id: str
    local_fields: list[str] = Field(min_length=1)
    target_dataset_id: str
    target_fields: list[str] = Field(min_length=1)
    description: str = ""

    _dataset_ids = field_validator("source_dataset_id", "target_dataset_id")(identifier)
    _field_names = field_validator("local_fields", "target_fields")(
        lambda values: [identifier(value) for value in values]
    )


class SemanticRelation(StrictModel):
    """本体关系先表达业务含义，再附加经过核验的物理路径。"""

    id: str
    source_entity_id: str
    target_entity_id: str
    predicate: str
    cardinality: Literal["many_to_one", "one_to_one", "one_to_many", "many_to_many"]
    description: str
    status: Literal["verified", "pending", "conflict"]
    path: RelationPath | None = None

    _names = field_validator("id", "source_entity_id", "target_entity_id", "predicate")(identifier)


class Metric(StrictModel):
    id: str
    name: str
    description: str
    aggregation: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    field: str
    unit: str = ""

    _names = field_validator("id", "field")(identifier)


class Relation(StrictModel):
    target_dataset_id: str
    local_fields: list[str] = Field(min_length=1)
    target_fields: list[str] = Field(min_length=1)
    cardinality: Literal["many_to_one", "one_to_one"]
    description: str


class TimePolicy(StrictModel):
    default_field: str
    timezone: str = "Asia/Shanghai"
    recent_days: int = Field(default=3, ge=1, le=3660)
    recent_mode: Literal["rolling_hours", "calendar_days"] = "rolling_hours"

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("无效时区") from exc
        return value


class Dataset(StrictModel):
    """程序生成的内部目录，不要求用户维护物理结构。"""

    id: str
    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    physical: Physical
    grain: str = "一行代表本表的一条记录，业务去重口径未另行定义"
    primary_key: list[str] = Field(default_factory=list)
    time_policy: TimePolicy | None = None
    fields: list[Column] = Field(min_length=1)
    metrics: list[Metric] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    _name = field_validator("id")(identifier)
    _entity = field_validator("entity_id")(lambda value: identifier(value) if value else value)
