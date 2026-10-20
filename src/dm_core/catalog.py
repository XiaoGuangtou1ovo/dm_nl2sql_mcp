"""物理描述与本体目录入口：业务语义人工维护，物理类型和主键由元数据生成。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from dm_core.config import AppError, Settings, digest, read_json, write_json
from dm_core.models import (
    Column,
    Dataset,
    Entity,
    EnumValue,
    Metric,
    Physical,
    Relation,
    RelationPath,
    SemanticRelation,
    SemanticRole,
    SemanticType,
    StrictModel,
    TimePolicy,
    identifier,
)


class FieldDescription(StrictModel):
    name: str | None = None
    description: str
    unit: str | None = None
    enum: list[EnumValue] = Field(default_factory=list)
    null_meaning: str | None = None
    entity_id: str | None = None
    property_id: str | None = None
    semantic_type: SemanticType | None = None
    semantic_role: SemanticRole | None = None
    synonyms: list[str] = Field(default_factory=list)


class JoinDescription(StrictModel):
    target: str
    on: dict[str, str]
    description: str


class Description(StrictModel):
    name: str
    description: str
    fields: dict[str, str | FieldDescription]
    aliases: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    grain: str = "每行是本表的一条记录，未定义业务去重口径"
    default_time: str | None = None
    metrics: list[Metric] = Field(default_factory=list)
    relations: list[JoinDescription] = Field(default_factory=list)


class JoinPathDescription(StrictModel):
    source_table: str
    target_table: str
    on: dict[str, str]
    description: str = ""


class SemanticRelationDescription(StrictModel):
    id: str
    source_entity: str
    target_entity: str
    predicate: str
    cardinality: Literal["many_to_one", "one_to_one", "one_to_many", "many_to_many"]
    description: str
    status: Literal["verified", "pending", "conflict"]
    path: JoinPathDescription | None = None


class OntologyDescription(StrictModel):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[SemanticRelationDescription] = Field(default_factory=list)


def split_table(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2:
        raise AppError("CONFIG", f"表名请写成模式.表：{value}")
    return identifier(parts[0]), identifier(parts[1])


def selected_tables(settings: Settings) -> list[str]:
    raw = read_json(settings.path("TABLES_PATH", "configs/tables.json"))
    if not isinstance(raw, dict) or set(raw) != {"tables"} or not isinstance(raw["tables"], list):
        raise AppError("CONFIG", '选表配置格式为{"tables": ["模式.表"]}')
    tables = raw["tables"]
    if not tables or not all(isinstance(t, str) for t in tables):
        raise AppError("CONFIG", "tables必须是非空的表名字符串列表")
    if len(set(tables)) != len(tables):
        raise AppError("CONFIG", "请至少选择一张表，且不能重复")
    for table in tables:
        split_table(table)
    return tables


def description_path(settings: Settings, table: str) -> Path:
    schema, name = split_table(table)
    return settings.path("DESCRIPTIONS_DIR", "configs/descriptions") / schema / f"{name}.json"


def dataset_id(table: str) -> str:
    # 标识符稳定且与大小写有关，避免不同模式/大小写同名表发生碰撞。
    return "table_" + digest(table)[:16]


def ontology_path(settings: Settings) -> Path:
    return settings.path("ONTOLOGY_PATH", "configs/ontology.json")


def description_source_hash(settings: Settings, descriptions: dict, ontology: dict) -> str:
    """目录指纹只包含会影响检索和 SQL 的输入，不记录 Excel 的样式或修改时间。"""
    identity = {key: settings.get(key) for key in ("DAMENG_HOST", "DAMENG_PORT", "DAMENG_USER")}
    return digest(
        {
            "tables": descriptions,
            "ontology": ontology,
            "database": identity,
            "timezone": settings.get("TIMEZONE", "Asia/Shanghai"),
        }
    )


def source_material(settings: Settings) -> tuple[dict, dict, str]:
    """描述变化、选表变化或切换数据库后，旧目录必须同步，不悄悄查询旧范围。"""
    tables = selected_tables(settings)
    descriptions = {table: read_json(description_path(settings, table)) for table in tables}
    path = ontology_path(settings)
    ontology = read_json(path) if path.exists() else {"entities": [], "relations": []}
    return descriptions, ontology, description_source_hash(settings, descriptions, ontology)


def supported_type(value: str) -> bool:
    return not any(
        t in value.upper() for t in ("CLOB", "BLOB", "BINARY", "IMAGE", "LONG", "XML", "OBJECT")
    )


def prepare_descriptions(
    settings: Settings, metadata: list[dict], tables: list[str] | None = None
) -> tuple[dict, dict]:
    """把达梦注释转换成可审阅描述，同时原样保留已有手工说明。"""
    requested = list(dict.fromkeys(tables or selected_tables(settings)))
    if not requested:
        raise AppError("CONFIG", "至少选择一张表")
    for table in requested:
        split_table(table)
    actual = {f"{item['schema']}.{item['table']}": item for item in metadata}
    missing = [table for table in requested if table not in actual]
    extra = [table for table in actual if table not in requested]
    if missing:
        raise AppError("SCHEMA_CHANGED", f"未取得表的元数据：{', '.join(missing)}")
    if extra:
        raise AppError("CONFIG", f"元数据超出选表范围：{', '.join(extra)}")

    descriptions = {}
    created, preserved, changes = [], [], []
    for table in requested:
        item = actual[table]
        path = description_path(settings, table)
        columns = {c["name"]: c for c in item["columns"]}
        if path.exists():
            try:
                raw = read_json(path)
                old = Description.model_validate(raw)
            except (ValueError, TypeError) as exc:
                raise AppError("CONFIG", f"{table}已有描述格式错误，请先修复") from exc
            descriptions[table] = raw
            preserved.append(table)
            changes.append(
                {
                    "table": table,
                    "not_described": sorted(set(columns) - set(old.fields)),
                    "missing_columns": sorted(set(old.fields) - set(columns)),
                }
            )
            continue
        supported = {
            name: column for name, column in columns.items() if supported_type(column["type"])
        }
        if not supported:
            raise AppError("CONFIG", f"{table}没有可安全查询的普通字段")
        fields = {
            name: _clean_comment(column.get("comment"))
            or f"{name}。数据库暂无字段注释，请补充中文业务含义。"
            for name, column in supported.items()
        }
        comment = _clean_comment(item.get("table_comment"))
        summaries = comment.splitlines() or [""]
        name = (summaries[0].split("。")[0].strip() or item["table"])[:60]
        descriptions[table] = {
            "name": name,
            "description": comment
            or f"{table}。数据库暂无表注释；请补充业务信息、记录粒度与使用边界。",
            "fields": fields,
        }
        created.append(table)
    report = {
        "created": created,
        "preserved": preserved,
        "changes": changes,
    }
    return descriptions, report


def _clean_comment(value) -> str:
    return str(value).strip() if value is not None else ""


def initialize(settings: Settings, metadata: list[dict], tables: list[str] | None = None) -> dict:
    """首次生成注释草稿；再次运行只报告差异，绝不覆盖人工解释或自动开放新列。"""
    descriptions, report = prepare_descriptions(settings, metadata, tables)
    for table in report["created"]:
        write_json(description_path(settings, table), descriptions[table])
    report["note"] = "新文件已预填数据库注释；删除不需要开放的字段，补充说明后catalog-sync。"
    return report


class Catalog:
    """原子索引包中的运行目录；模型只看到描述文件明确选择的表和列。"""

    def __init__(self, raw: dict):
        try:
            values = [Dataset.model_validate(d) for d in raw["datasets"]]
            ontology_raw = raw.get("ontology", {"entities": [], "relations": []})
            entities = [Entity.model_validate(item) for item in ontology_raw.get("entities", [])]
            semantic_relations = [
                SemanticRelation.model_validate(item) for item in ontology_raw.get("relations", [])
            ]
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            raise AppError("CATALOG_INVALID", "内部目录无效，请重新catalog-sync") from exc
        self.datasets = {d.id: d for d in values}
        if not self.datasets or len(self.datasets) != len(values):
            raise AppError("CATALOG_INVALID", "目录为空或数据集ID重复")
        self.entities = {entity.id: entity for entity in entities}
        if len(self.entities) != len(entities):
            raise AppError("CATALOG_INVALID", "本体实体ID重复")
        self.semantic_relations = {relation.id: relation for relation in semantic_relations}
        if len(self.semantic_relations) != len(semantic_relations):
            raise AppError("CATALOG_INVALID", "本体关系ID重复")
        self._validate_ontology()
        self.raw = raw
        self.hash = digest(raw)
        self.version = self.hash[:12]

    def _validate_ontology(self):
        """本体关系和物理路径必须同时可解释，但不把待确认关系转成SQL关联。"""
        for dataset in self.datasets.values():
            if dataset.entity_id and dataset.entity_id not in self.entities:
                raise AppError("CATALOG_INVALID", "数据集引用了不存在的实体")
            for field in dataset.fields:
                if field.entity_id and field.entity_id not in self.entities:
                    raise AppError("CATALOG_INVALID", "字段引用了不存在的实体")
                if bool(field.entity_id) != bool(field.property_id):
                    raise AppError("CATALOG_INVALID", "实体和属性映射必须同时存在")
        for relation in self.semantic_relations.values():
            if (
                relation.source_entity_id not in self.entities
                or relation.target_entity_id not in self.entities
            ):
                raise AppError("CATALOG_INVALID", "本体关系引用了不存在的实体")
            if relation.status == "verified":
                if relation.path is None:
                    raise AppError("CATALOG_INVALID", "已核验本体关系缺少物理路径")
                if relation.cardinality not in {"many_to_one", "one_to_one"}:
                    raise AppError("CATALOG_INVALID", "当前SQL只支持已核验的多对一/一对一关系")
                self._validate_relation_path(relation.path)
            elif relation.path is not None:
                raise AppError("CATALOG_INVALID", "待确认或冲突关系不能包含生效的物理路径")

    def _validate_relation_path(self, path: RelationPath):
        source = self.get(path.source_dataset_id)
        target = self.get(path.target_dataset_id)
        local = {field.name for field in source.fields}
        target_fields = {field.name for field in target.fields}
        if (
            len(path.local_fields) != len(path.target_fields)
            or not set(path.local_fields).issubset(local)
            or not set(path.target_fields).issubset(target_fields)
            or not target.primary_key
            or path.target_fields != target.primary_key
        ):
            raise AppError("CATALOG_INVALID", "物理关联路径字段或目标主键无效")

    def get(self, key):
        if key not in self.datasets:
            raise AppError("DATASET_DENIED", "数据集不在选表范围", denied=True)
        return self.datasets[key]

    def context(self, ids):
        """给规划模型提供当前候选数据集的局部实体子图，而非整个本体。"""
        result = []
        for key in ids:
            dataset = self.get(key)
            entity_ids = {
                field.entity_id for field in dataset.fields if field.entity_id is not None
            }
            if dataset.entity_id:
                entity_ids.add(dataset.entity_id)
            for relation in self.semantic_relations.values():
                if relation.source_entity_id in entity_ids:
                    entity_ids.add(relation.target_entity_id)
                if relation.target_entity_id in entity_ids:
                    entity_ids.add(relation.source_entity_id)
            entities = []
            for entity_id in sorted(entity_ids):
                entity = self.entities.get(entity_id)
                if not entity:
                    continue
                mappings = [
                    {
                        "dataset_id": dataset.id,
                        "field": field.name,
                        "property_id": field.property_id,
                        "semantic_type": field.semantic_type,
                        "semantic_role": field.semantic_role,
                    }
                    for field in dataset.fields
                    if field.entity_id == entity_id
                ]
                entities.append({**entity.model_dump(), "field_mappings": mappings})
            relations = [
                relation.model_dump()
                for relation in self.semantic_relations.values()
                if relation.source_entity_id in entity_ids
                or relation.target_entity_id in entity_ids
            ]
            result.append(
                {
                    **dataset.model_dump(),
                    "ontology": {
                        "primary_entity_id": dataset.entity_id,
                        "entities": entities,
                        "semantic_relations": relations,
                    },
                }
            )
        return result

    def documents(self):
        records = []
        for d in self.datasets.values():
            prefix = f"{d.physical.schema_name}.{d.physical.table} {d.name} {d.description} {' '.join(d.aliases)}"
            records.append(
                {"id": f"{d.id}:table", "dataset_id": d.id, "text": f"{prefix} {d.grain}"}
            )
            for f in d.fields:
                entity = self.entities.get(f.entity_id) if f.entity_id else None
                ontology_text = " ".join(
                    item
                    for item in (
                        entity.name if entity else "",
                        " ".join(entity.aliases) if entity else "",
                        f.entity_id or "",
                        f.property_id or "",
                        f.semantic_type or "",
                        f.semantic_role or "",
                        " ".join(f.synonyms),
                    )
                    if item
                )
                records.append(
                    {
                        "id": f"{d.id}:field:{f.name}",
                        "dataset_id": d.id,
                        "text": f"{prefix} {f.name} {f.business_name} {f.description} {f.unit or ''} "
                        + f"{ontology_text} "
                        + " ".join(f"{e.label}={e.value}" for e in f.enum),
                    }
                )
            for m in d.metrics:
                records.append(
                    {
                        "id": f"{d.id}:metric:{m.id}",
                        "dataset_id": d.id,
                        "text": f"{prefix} {m.name} {m.description} {m.aggregation} {m.field}",
                    }
                )
            entity_ids = {f.entity_id for f in d.fields if f.entity_id}
            if d.entity_id:
                entity_ids.add(d.entity_id)
            for relation in self.semantic_relations.values():
                if relation.source_entity_id in entity_ids:
                    entity_ids.add(relation.target_entity_id)
                if relation.target_entity_id in entity_ids:
                    entity_ids.add(relation.source_entity_id)
            for entity_id in sorted(entity_ids):
                entity = self.entities.get(entity_id)
                if not entity:
                    continue
                relations = [
                    f"{relation.predicate} {relation.description} {relation.status}"
                    for relation in self.semantic_relations.values()
                    if entity.id in {relation.source_entity_id, relation.target_entity_id}
                ]
                records.append(
                    {
                        "id": f"{d.id}:entity:{entity.id}",
                        "dataset_id": d.id,
                        "text": f"{prefix} {entity.name} {entity.description} "
                        + f"{' '.join(entity.aliases)} {entity.grain or ''} {' '.join(relations)}",
                    }
                )
        return records


def build_catalog(
    settings: Settings,
    metadata: list[dict],
    descriptions: dict | None = None,
    ontology: dict | None = None,
) -> tuple[Catalog, str]:
    """可由已发布 JSON 或 Excel 内存结果构建，后者让实库核验发生在写文件之前。"""
    if descriptions is None:
        descriptions, ontology, source_hash = source_material(settings)
    else:
        ontology = ontology or {"entities": [], "relations": []}
        source_hash = description_source_hash(settings, descriptions, ontology)
    try:
        ontology_desc = OntologyDescription.model_validate(ontology)
    except ValidationError as exc:
        raise AppError("CONFIG", f"本体格式错误：{exc.errors()[0]['loc']}") from exc
    entities = {entity.id: entity for entity in ontology_desc.entities}
    if len(entities) != len(ontology_desc.entities):
        raise AppError("CONFIG", "本体实体ID重复")
    if len({relation.id for relation in ontology_desc.relations}) != len(ontology_desc.relations):
        raise AppError("CONFIG", "本体关系ID重复")
    actual_tables = {f"{m['schema']}.{m['table']}": m for m in metadata}
    datasets = []
    for table, raw in descriptions.items():
        try:
            desc = Description.model_validate(raw)
        except ValidationError as exc:
            raise AppError("CONFIG", f"{table}描述格式错误：{exc.errors()[0]['loc']}") from exc
        if table not in actual_tables:
            raise AppError("SCHEMA_CHANGED", f"未取得{table}的元数据")
        meta = actual_tables[table]
        actual = {c["name"]: c for c in meta["columns"]}
        if not desc.fields:
            raise AppError("CONFIG", f"{table}至少需要一个描述字段")
        fields = []
        for name, info in desc.fields.items():
            identifier(name)
            if name not in actual:
                raise AppError("SCHEMA_CHANGED", f"{table}.{name}不存在；请核对大小写或移除描述")
            physical = actual[name]
            if not supported_type(physical["type"]):
                raise AppError("CONFIG", f"{table}.{name}的{physical['type']}类型暂不支持查询")
            detail = (
                info if isinstance(info, FieldDescription) else FieldDescription(description=info)
            )
            if not detail.description.strip():
                raise AppError("CONFIG", f"{table}.{name}的描述不能为空")
            if bool(detail.entity_id) != bool(detail.property_id):
                raise AppError("CONFIG", f"{table}.{name}的entity_id和property_id必须同时填写")
            if detail.entity_id and detail.entity_id not in entities:
                raise AppError("CONFIG", f"{table}.{name}引用了不存在的实体：{detail.entity_id}")
            fields.append(
                Column(
                    name=name,
                    physical_type=physical["type"],
                    business_name=detail.name or detail.description.split("。")[0][:60],
                    description=detail.description,
                    nullable=physical["nullable"],
                    entity_id=detail.entity_id,
                    property_id=detail.property_id,
                    semantic_type=detail.semantic_type,
                    semantic_role=detail.semantic_role,
                    synonyms=detail.synonyms,
                    unit=detail.unit,
                    enum=detail.enum,
                    null_meaning=detail.null_meaning,
                )
            )
        time_policy = None
        if desc.default_time:
            column = next((f for f in fields if f.name == desc.default_time), None)
            if column is None or not any(
                t in column.physical_type.upper() for t in ("DATE", "TIMESTAMP")
            ):
                raise AppError("CONFIG", f"{table}的default_time必须是已描述的日期时间列")
            column.role = "event_time"
            time_policy = TimePolicy(
                default_field=desc.default_time, timezone=settings.get("TIMEZONE", "Asia/Shanghai")
            )
        names = {f.name for f in fields}
        if len({m.id for m in desc.metrics}) != len(desc.metrics):
            raise AppError("CONFIG", f"{table}指标ID重复")
        for metric in desc.metrics:
            if metric.field not in names:
                raise AppError("CONFIG", f"{table}指标引用了未描述的字段：{metric.field}")
            column = next(f for f in fields if f.name == metric.field)
            if metric.aggregation in {"sum", "avg"} and not any(
                t in column.physical_type.upper()
                for t in ("NUMBER", "INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")
            ):
                raise AppError("CONFIG", f"{table}.{metric.field}不是可求和的数值字段")
        schema, name = split_table(table)
        if desc.entity_id and desc.entity_id not in entities:
            raise AppError("CONFIG", f"{table}引用了不存在的实体：{desc.entity_id}")
        datasets.append(
            Dataset(
                id=dataset_id(table),
                name=desc.name,
                description=desc.description,
                aliases=desc.aliases,
                entity_id=desc.entity_id,
                grain=desc.grain,
                fields=fields,
                physical=Physical(datasource_id="default", schema=schema, table=name),
                primary_key=meta["primary_key"],
                time_policy=time_policy,
                metrics=desc.metrics,
            )
        )
    by_id = {d.id: d for d in datasets}
    for table, raw in descriptions.items():
        desc = Description.model_validate(raw)
        base = by_id[dataset_id(table)]
        targets = set()
        for relation in desc.relations:
            if relation.target not in descriptions or not relation.on or relation.target in targets:
                raise AppError("CONFIG", f"{table}关联目标未选择、键为空或目标重复")
            targets.add(relation.target)
            target = by_id[dataset_id(relation.target)]
            local, remote = list(relation.on), list(relation.on.values())
            if (
                not set(local).issubset(f.name for f in base.fields)
                or not set(remote).issubset(f.name for f in target.fields)
                or not target.primary_key
                or remote != target.primary_key
            ):
                raise AppError("CONFIG", f"{table}关联键必须已描述，且完整匹配目标真实主键")
            base.relations.append(
                Relation(
                    target_dataset_id=target.id,
                    local_fields=local,
                    target_fields=remote,
                    cardinality="many_to_one",
                    description=relation.description,
                )
            )

    semantic_relations = []
    allowed_cardinalities = {"many_to_one", "one_to_one", "one_to_many", "many_to_many"}
    allowed_statuses = {"verified", "pending", "conflict"}
    for source in ontology_desc.relations:
        if source.source_entity not in entities or source.target_entity not in entities:
            raise AppError("CONFIG", f"本体关系{source.id}引用了不存在的实体")
        if source.status not in allowed_statuses:
            raise AppError("CONFIG", f"本体关系{source.id}的status无效")
        if source.cardinality not in allowed_cardinalities:
            raise AppError("CONFIG", f"本体关系{source.id}的cardinality无效")
        if source.status == "verified":
            if source.path is None:
                raise AppError("CONFIG", f"已核验本体关系{source.id}必须提供物理路径")
            if source.cardinality not in {"many_to_one", "one_to_one"}:
                raise AppError("CONFIG", f"当前版本只执行已核验的多对一/一对一关系：{source.id}")
            if (
                source.path.source_table not in descriptions
                or source.path.target_table not in descriptions
            ):
                raise AppError("CONFIG", f"本体关系{source.id}的物理路径引用了未选表")
            source_dataset = by_id[dataset_id(source.path.source_table)]
            target_dataset = by_id[dataset_id(source.path.target_table)]
            local, remote = list(source.path.on), list(source.path.on.values())
            if (
                not source.path.on
                or not set(local).issubset(f.name for f in source_dataset.fields)
                or not set(remote).issubset(f.name for f in target_dataset.fields)
                or not target_dataset.primary_key
                or remote != target_dataset.primary_key
            ):
                raise AppError("CONFIG", f"本体关系{source.id}的路径字段无效或未完整匹配目标主键")
            if any(
                relation.target_dataset_id == target_dataset.id
                for relation in source_dataset.relations
            ):
                raise AppError(
                    "CONFIG",
                    f"{source.path.source_table}到{source.path.target_table}存在重复物理关联",
                )
            path = RelationPath(
                source_dataset_id=source_dataset.id,
                local_fields=local,
                target_dataset_id=target_dataset.id,
                target_fields=remote,
                description=source.path.description,
            )
            source_dataset.relations.append(
                Relation(
                    target_dataset_id=target_dataset.id,
                    local_fields=local,
                    target_fields=remote,
                    cardinality=source.cardinality,
                    description=source.description,
                )
            )
        elif source.path is not None:
            raise AppError("CONFIG", f"待确认或冲突关系{source.id}不能包含生效的物理路径")
        else:
            path = None
        semantic_relations.append(
            SemanticRelation(
                id=source.id,
                source_entity_id=source.source_entity,
                target_entity_id=source.target_entity,
                predicate=source.predicate,
                cardinality=source.cardinality,
                description=source.description,
                status=source.status,
                path=path,
            )
        )
    final_ontology = {
        "entities": [entity.model_dump() for entity in ontology_desc.entities],
        "relations": [relation.model_dump() for relation in semantic_relations],
    }
    return (
        Catalog(
            {
                "datasets": [d.model_dump() for d in datasets],
                "ontology": final_ontology,
            }
        ),
        source_hash,
    )
