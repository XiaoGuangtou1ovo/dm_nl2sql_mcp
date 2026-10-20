"""Excel 描述清单：把业务人员维护的工作簿转换成程序稳定读取的 JSON。

工作簿由六类记录组成：

- Tables：一行一张表，声明模式、表名、业务名称和说明。
- Entities：一行一个业务实体。
- Fields：一行一个字段，声明需要向模型开放的字段及业务说明。
- Relations：可选，一行一个业务实体之间的语义关系。
- JoinPaths：可选，保存已核验语义关系的物理关联路径。
- Metrics：可选，一行一个业务指标，用于限制可执行的聚合。

只有出现在 Fields 中的列才会被开放；删除表格行或字段行即可撤销授权。
物理类型、可空性和主键仍从达梦实时读取，不需要写进 Excel。
"""

import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from dm_core.catalog import (
    Description,
    OntologyDescription,
    description_path,
    ontology_path,
    split_table,
)
from dm_core.config import AppError, Settings, write_json
from dm_core.models import identifier

TABLE_HEADERS = {
    "schema",
    "table",
    "name",
    "description",
    "aliases",
    "grain",
    "default_time",
    "entity_id",
}
TABLE_REQUIRED = {"schema", "table", "name", "description"}
FIELD_HEADERS = {
    "schema",
    "table",
    "field",
    "name",
    "description",
    "unit",
    "enum",
    "null_meaning",
    "entity_id",
    "property_id",
    "semantic_type",
    "semantic_role",
    "synonyms",
}
FIELD_REQUIRED = {"schema", "table", "field", "description"}
ENTITY_HEADERS = {"entity_id", "name", "description", "aliases", "grain", "status"}
ENTITY_REQUIRED = {"entity_id", "name", "description"}
RELATION_HEADERS = {
    "relation_id",
    "source_entity",
    "target_entity",
    "predicate",
    "cardinality",
    "description",
    "status",
}
RELATION_REQUIRED = RELATION_HEADERS
JOIN_PATH_HEADERS = {
    "relation_id",
    "path_id",
    "schema",
    "table",
    "local_field",
    "target",
    "target_field",
    "description",
}
JOIN_PATH_REQUIRED = {
    "relation_id",
    "path_id",
    "schema",
    "table",
    "local_field",
    "target",
    "target_field",
}
METRIC_HEADERS = {"schema", "table", "id", "name", "description", "aggregation", "field", "unit"}
METRIC_REQUIRED = {"schema", "table", "id", "name", "description", "aggregation", "field"}


@dataclass(frozen=True)
class WorkbookSpec:
    descriptions: dict
    ontology: dict


def _text(value) -> str:
    """Excel 空单元格统一为空串，其余值保留为可读文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheet_records(workbook, name: str, allowed: set[str], required: set[str]) -> list[dict]:
    """读取一个标准工作表，并在进入业务校验前拒绝缺列、错列和重复表头。"""
    if name not in workbook.sheetnames:
        raise AppError("EXCEL_INVALID", f"工作簿缺少{name}工作表")
    sheet = workbook[name]
    rows = sheet.iter_rows(values_only=True)
    try:
        header_values = next(rows)
    except StopIteration as exc:
        raise AppError("EXCEL_INVALID", f"{name}工作表为空") from exc
    headers = [_text(value) for value in header_values]
    while headers and not headers[-1]:
        headers.pop()
    if not headers:
        raise AppError("EXCEL_INVALID", f"{name}工作表缺少表头")
    if len(headers) != len(set(headers)):
        raise AppError("EXCEL_INVALID", f"{name}工作表存在重复表头")
    missing = sorted(required - set(headers))
    unknown = sorted(set(headers) - allowed)
    if missing:
        raise AppError("EXCEL_INVALID", f"{name}工作表缺少列：{', '.join(missing)}")
    if unknown:
        raise AppError("EXCEL_INVALID", f"{name}工作表存在未知列：{', '.join(unknown)}")

    records = []
    for row_number, values in enumerate(rows, start=2):
        values = list(values[: len(headers)])
        values.extend([None] * (len(headers) - len(values)))
        if not any(_text(value) for value in values):
            continue
        record = {header: _text(value) for header, value in zip(headers, values, strict=True)}
        record["_row"] = row_number
        records.append(record)
    return records


def _required(record: dict, name: str, sheet: str) -> str:
    value = record[name]
    if not value:
        raise AppError("EXCEL_INVALID", f"{sheet}第{record['_row']}行的{name}不能为空")
    return value


def _table_key(record: dict, sheet: str) -> str:
    schema = _required(record, "schema", sheet)
    table = _required(record, "table", sheet)
    split_table(f"{schema}.{table}")
    return f"{schema}.{table}"


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,，;；]+", value) if item.strip()]


def _enum_scalar(value: str):
    """枚举值尽量保留数字/布尔类型，带前导零的代码仍按字符串保存。"""
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"-?(0|[1-9]\d*)", value):
        return int(value)
    if re.fullmatch(r"-?(0|[1-9]\d*)\.\d+", value):
        return float(value)
    return value


def _enum_values(value: str, location: str) -> list[dict]:
    """每项写成“真实值=中文含义”，用换行、分号或竖线分隔。"""
    result = []
    for item in [part.strip() for part in re.split(r"[\n;；|]+", value) if part.strip()]:
        if "=" not in item and "：" not in item:
            raise AppError("EXCEL_INVALID", f"{location}的枚举项应为“真实值=中文含义”")
        separator = "=" if "=" in item else "："
        code, label = [part.strip() for part in item.split(separator, 1)]
        if not code or not label:
            raise AppError("EXCEL_INVALID", f"{location}的枚举项缺少值或含义")
        result.append({"value": _enum_scalar(code), "label": label})
    return result


def read_workbook(path: Path) -> WorkbookSpec:
    """完整解析并交叉校验工作簿，返回物理描述和轻量本体。"""
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (OSError, InvalidFileException, ValueError) as exc:
        raise AppError("EXCEL_INVALID", f"无法读取Excel：{path}") from exc

    try:
        table_rows = _sheet_records(workbook, "Tables", TABLE_HEADERS, TABLE_REQUIRED)
        field_rows = _sheet_records(workbook, "Fields", FIELD_HEADERS, FIELD_REQUIRED)
        entity_rows = (
            _sheet_records(workbook, "Entities", ENTITY_HEADERS, ENTITY_REQUIRED)
            if "Entities" in workbook.sheetnames
            else []
        )
        relation_rows = (
            _sheet_records(workbook, "Relations", RELATION_HEADERS, RELATION_REQUIRED)
            if "Relations" in workbook.sheetnames
            else []
        )
        join_path_rows = (
            _sheet_records(workbook, "JoinPaths", JOIN_PATH_HEADERS, JOIN_PATH_REQUIRED)
            if "JoinPaths" in workbook.sheetnames
            else []
        )
        metric_rows = (
            _sheet_records(workbook, "Metrics", METRIC_HEADERS, METRIC_REQUIRED)
            if "Metrics" in workbook.sheetnames
            else []
        )
    finally:
        workbook.close()

    entities = []
    entity_by_id = {}
    for record in entity_rows:
        entity_id = _required(record, "entity_id", "Entities")
        identifier(entity_id)
        if entity_id in entity_by_id:
            raise AppError("EXCEL_INVALID", f"Entities第{record['_row']}行重复声明{entity_id}")
        entity = {
            "id": entity_id,
            "name": _required(record, "name", "Entities"),
            "description": _required(record, "description", "Entities"),
            "aliases": _split_list(record["aliases"]),
            "status": record["status"] or "pending",
        }
        if record["grain"]:
            entity["grain"] = record["grain"]
        entity_by_id[entity_id] = entity
        entities.append(entity)

    descriptions: OrderedDict[str, dict] = OrderedDict()
    for record in table_rows:
        table = _table_key(record, "Tables")
        if table in descriptions:
            raise AppError("EXCEL_INVALID", f"Tables第{record['_row']}行重复声明{table}")
        raw = {
            "name": _required(record, "name", "Tables"),
            "description": _required(record, "description", "Tables"),
            "fields": OrderedDict(),
        }
        aliases = _split_list(record["aliases"])
        if aliases:
            raw["aliases"] = aliases
        if record["grain"]:
            raw["grain"] = record["grain"]
        if record["default_time"]:
            raw["default_time"] = record["default_time"]
        if record["entity_id"]:
            identifier(record["entity_id"])
            if record["entity_id"] not in entity_by_id:
                raise AppError(
                    "EXCEL_INVALID",
                    f"Tables第{record['_row']}行引用了不存在的实体{record['entity_id']}",
                )
            raw["entity_id"] = record["entity_id"]
        descriptions[table] = raw

    field_names: dict[str, set[str]] = {table: set() for table in descriptions}
    for record in field_rows:
        table = _table_key(record, "Fields")
        location = f"Fields第{record['_row']}行"
        if table not in descriptions:
            raise AppError("EXCEL_INVALID", f"{location}引用了Tables中不存在的{table}")
        field = _required(record, "field", "Fields")
        identifier(field)
        if field in field_names[table]:
            raise AppError("EXCEL_INVALID", f"{location}重复声明{table}.{field}")
        field_names[table].add(field)
        description = _required(record, "description", "Fields")
        detail = {"description": description}
        if record["name"]:
            detail["name"] = record["name"]
        if record["unit"]:
            detail["unit"] = record["unit"]
        if record["null_meaning"]:
            detail["null_meaning"] = record["null_meaning"]
        if record["enum"]:
            detail["enum"] = _enum_values(record["enum"], location)
        entity_id = record["entity_id"]
        property_id = record["property_id"]
        if bool(entity_id) != bool(property_id):
            raise AppError("EXCEL_INVALID", f"{location}的entity_id和property_id必须同时填写")
        if entity_id:
            identifier(entity_id)
            identifier(property_id)
            if entity_id not in entity_by_id:
                raise AppError("EXCEL_INVALID", f"{location}引用了不存在的实体{entity_id}")
            detail["entity_id"] = entity_id
            detail["property_id"] = property_id
        if record["semantic_type"]:
            detail["semantic_type"] = record["semantic_type"]
        if record["semantic_role"]:
            detail["semantic_role"] = record["semantic_role"]
        synonyms = _split_list(record["synonyms"])
        if synonyms:
            detail["synonyms"] = synonyms
        # 没有可选语义时继续写成字符串，避免无意义地扩大配置并改变目录指纹。
        descriptions[table]["fields"][field] = (
            description if list(detail) == ["description"] else detail
        )

    for table, description in descriptions.items():
        if not description["fields"]:
            raise AppError("EXCEL_INVALID", f"{table}在Fields中没有可开放字段")
        time_field = description.get("default_time")
        if time_field and time_field not in description["fields"]:
            raise AppError("EXCEL_INVALID", f"{table}的default_time必须是Fields中的已开放字段")

    semantic_relations: OrderedDict[str, dict] = OrderedDict()
    for record in relation_rows:
        relation_id = _required(record, "relation_id", "Relations")
        identifier(relation_id)
        source_entity = _required(record, "source_entity", "Relations")
        target_entity = _required(record, "target_entity", "Relations")
        identifier(source_entity)
        identifier(target_entity)
        if source_entity not in entity_by_id or target_entity not in entity_by_id:
            raise AppError("EXCEL_INVALID", "Relations引用了Entities中不存在的实体")
        if source_entity == target_entity:
            raise AppError("EXCEL_INVALID", "Relations的源实体和目标实体不能相同")
        if relation_id in semantic_relations:
            raise AppError("EXCEL_INVALID", f"Relations重复声明关系{relation_id}")
        semantic_relations[relation_id] = {
            "id": relation_id,
            "source_entity": source_entity,
            "target_entity": target_entity,
            "predicate": _required(record, "predicate", "Relations"),
            "cardinality": _required(record, "cardinality", "Relations"),
            "description": _required(record, "description", "Relations"),
            "status": _required(record, "status", "Relations"),
            "path": None,
        }

    path_groups: OrderedDict[tuple[str, str], dict] = OrderedDict()
    for record in join_path_rows:
        relation_id = _required(record, "relation_id", "JoinPaths")
        path_id = _required(record, "path_id", "JoinPaths")
        identifier(path_id)
        if relation_id not in semantic_relations:
            raise AppError("EXCEL_INVALID", f"JoinPaths引用了不存在的本体关系{relation_id}")
        source = _table_key(record, "JoinPaths")
        target = _required(record, "target", "JoinPaths")
        split_table(target)
        if source not in descriptions or target not in descriptions:
            raise AppError("EXCEL_INVALID", "JoinPaths中的源表和目标表都必须出现在Tables中")
        local_field = _required(record, "local_field", "JoinPaths")
        target_field = _required(record, "target_field", "JoinPaths")
        if local_field not in field_names[source] or target_field not in field_names[target]:
            raise AppError("EXCEL_INVALID", "JoinPaths中的字段必须先在Fields中开放")
        key = (relation_id, path_id)
        path = path_groups.setdefault(
            key,
            {
                "source_table": source,
                "target_table": target,
                "on": {},
                "description": record["description"],
            },
        )
        if (
            path["source_table"] != source
            or path["target_table"] != target
            or path["description"] != record["description"]
        ):
            raise AppError("EXCEL_INVALID", f"JoinPaths关系{relation_id}/{path_id}的组合不一致")
        if local_field in path["on"]:
            raise AppError("EXCEL_INVALID", f"JoinPaths第{record['_row']}行重复关联字段")
        path["on"][local_field] = target_field

    for relation_id, relation in semantic_relations.items():
        paths = [
            path for (candidate_id, _), path in path_groups.items() if candidate_id == relation_id
        ]
        if relation["status"] == "verified":
            if len(paths) != 1:
                raise AppError(
                    "EXCEL_INVALID", f"已核验关系{relation_id}必须且只能配置一个物理路径"
                )
            relation["path"] = paths[0]
        elif paths:
            raise AppError("EXCEL_INVALID", f"待确认或冲突关系{relation_id}不能配置物理路径")

    existing_metric_ids: dict[str, set[str]] = {table: set() for table in descriptions}
    for record in metric_rows:
        table = _table_key(record, "Metrics")
        if table not in descriptions:
            raise AppError("EXCEL_INVALID", "Metrics引用了Tables中不存在的表")
        metric_id = _required(record, "id", "Metrics")
        identifier(metric_id)
        if metric_id in existing_metric_ids[table]:
            raise AppError("EXCEL_INVALID", f"{table}的指标ID重复：{metric_id}")
        existing_metric_ids[table].add(metric_id)
        descriptions[table].setdefault("metrics", []).append(
            {
                "id": metric_id,
                "name": _required(record, "name", "Metrics"),
                "description": _required(record, "description", "Metrics"),
                "aggregation": _required(record, "aggregation", "Metrics"),
                "field": _required(record, "field", "Metrics"),
                "unit": record["unit"],
            }
        )

    # 此时再做 Pydantic 校验，确保写文件前已发现所有结构错误。
    for table, raw in descriptions.items():
        try:
            Description.model_validate(raw)
        except ValueError as exc:
            raise AppError("EXCEL_INVALID", f"{table}描述校验失败：{exc}") from exc
    ontology = {
        "entities": entities,
        "relations": list(semantic_relations.values()),
    }
    try:
        OntologyDescription.model_validate(ontology)
    except ValueError as exc:
        raise AppError("EXCEL_INVALID", f"本体校验失败：{exc}") from exc
    return WorkbookSpec(dict(descriptions), ontology)


def read_description_workbook(path: Path) -> dict[str, dict]:
    """兼容只需要物理描述的调用方；完整导入使用 read_workbook。"""
    return read_workbook(path).descriptions


def _atomic_write(path: Path, value):
    """单个 JSON 文件先写临时文件再替换，避免留下半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    write_json(temporary, value)
    os.replace(temporary, path)


def import_description_workbook(settings: Settings, excel_path: Path) -> dict:
    """校验并更新选表清单和 JSON 描述；供测试和 JSON 迁移流程使用。"""
    spec = read_workbook(excel_path)
    summary = persist_description_workbook(settings, spec.descriptions, spec.ontology)
    summary["source"] = str(excel_path)
    return summary


def persist_description_workbook(settings: Settings, descriptions: dict, ontology: dict) -> dict:
    """目录和索引成功后发布描述；返回供 CLI 展示的摘要。"""
    tables_path = settings.path("TABLES_PATH", "configs/tables.json")
    # 先发布描述，最后才切换选表清单；中途失败时旧选表仍只引用完整描述。
    for table, raw in descriptions.items():
        _atomic_write(description_path(settings, table), raw)
    _atomic_write(ontology_path(settings), ontology)
    _atomic_write(tables_path, {"tables": list(descriptions)})
    return {
        "tables": len(descriptions),
        "fields": sum(len(raw["fields"]) for raw in descriptions.values()),
        "metrics": sum(len(raw.get("metrics", [])) for raw in descriptions.values()),
        "entities": len(ontology.get("entities", [])),
        "relations": len(ontology.get("relations", [])),
    }
