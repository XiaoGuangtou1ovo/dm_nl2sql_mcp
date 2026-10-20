"""自动发现、选表保护和注释草稿生成。"""

import pytest

from dm_core.bootstrap import bootstrap_catalog
from dm_core.catalog import description_path, prepare_descriptions
from dm_core.config import AppError, read_json
from dm_core.discovery import filter_table_refs, parse_table_list, table_refs


def test_table_filters_support_schema_and_bare_name_patterns():
    refs = table_refs(
        [
            {"schema": "KHFW", "table": "BT_CHBSD"},
            {"schema": "KHFW", "table": "BT_CHBHW_DD"},
            {"schema": "KHFW", "table": "TMP_DEBUG"},
            {"schema": "ZKTOS", "table": "BT_YDCHBSHB"},
        ]
    )
    selected = filter_table_refs(
        refs,
        include=["BT_*"],
        exclude=["*TMP*"],
        max_tables=10,
    )
    assert selected == ["KHFW.BT_CHBHW_DD", "KHFW.BT_CHBSD", "ZKTOS.BT_YDCHBSHB"]
    assert parse_table_list("KHFW.BT_CHBSD, ZKTOS.BT_YDCHBSHB") == [
        "KHFW.BT_CHBSD",
        "ZKTOS.BT_YDCHBSHB",
    ]
    with pytest.raises(AppError, match="超过上限"):
        filter_table_refs(refs, include=["*"], exclude=[], max_tables=1)


def test_prepare_descriptions_uses_database_comments_and_preserves_existing(settings, metadata):
    event_before = description_path(settings, "S.EVENT").read_bytes()
    description_path(settings, "S.DEVICE").unlink()
    metadata[0]["table_comment"] = "业务事件记录表"
    metadata[1]["table_comment"] = "设备主档"
    metadata[1]["columns"][1]["comment"] = "设备中文名称"
    metadata[1]["columns"].append(
        {"name": "PAYLOAD", "type": "CLOB", "nullable": True, "comment": "大文本"}
    )

    descriptions, report = prepare_descriptions(settings, metadata, ["S.EVENT", "S.DEVICE"])

    assert description_path(settings, "S.EVENT").read_bytes() == event_before
    assert report["preserved"] == ["S.EVENT"]
    assert report["created"] == ["S.DEVICE"]
    assert descriptions["S.EVENT"]["description"] == "业务事件记录"
    assert descriptions["S.DEVICE"]["name"] == "设备主档"
    assert descriptions["S.DEVICE"]["fields"]["NAME"] == "设备中文名称"
    assert "PAYLOAD" not in descriptions["S.DEVICE"]["fields"]


def test_bootstrap_without_index_publishes_new_table(settings):
    metadata = [
        {
            "schema": "S",
            "table": "ALERT",
            "table_comment": "设备告警记录",
            "primary_key": ["ID"],
            "columns": [
                {
                    "name": "ID",
                    "type": "NUMBER",
                    "nullable": False,
                    "comment": "告警记录主键",
                },
                {
                    "name": "ALERT_TEXT",
                    "type": "VARCHAR2",
                    "nullable": True,
                    "comment": "告警内容",
                },
            ],
        }
    ]

    class Database:
        def metadata(self, tables):
            assert tables == [("S", "ALERT")]
            return metadata

    result = bootstrap_catalog(
        settings,
        Database(),
        schemas=None,
        tables=["S.ALERT"],
        include=None,
        exclude=None,
        keep_existing=False,
        max_tables=10,
        build_index=False,
        inventory_output="artifacts/discovery.json",
    )
    assert result["status"] == "success"
    assert result["index"]["action"] == "skipped"
    assert read_json(settings.root / "configs/tables.json") == {"tables": ["S.ALERT"]}
    generated = read_json(description_path(settings, "S.ALERT"))
    assert generated["name"] == "设备告警记录"
    assert generated["fields"]["ALERT_TEXT"] == "告警内容"
    assert (settings.root / "artifacts/discovery.json").is_file()
