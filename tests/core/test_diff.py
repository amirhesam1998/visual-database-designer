"""Conformance kit — Diff Engine (``docs/spec-diff-engine.md`` §11).

Rename is one ``rename_*`` op (not drop+add); a presentation-only change yields an EMPTY op list;
create-table sorts before its FK; a change_type emits typed from/to; three-way detects same-attribute
conflicts.
"""

from __future__ import annotations

import copy

import pytest

from app.core import schema_json as sj
from app.core.diff import diff, three_way_diff

from .factory import FLD_O_TOTAL, TBL_ORDERS, canonical_schema


@pytest.fixture
def base():
    return canonical_schema()


def _ops(a, b):
    return diff(sj.load(a, validate=False), sj.load(b, validate=False)).operations


def test_identity_diff_is_empty(base):
    assert diff(sj.load(base), sj.load(base)).operations == []


def test_presentation_only_change_is_ignored(base):
    moved = copy.deepcopy(base)
    moved["presentation"]["nodes"][0]["x"] = 9999
    moved["presentation"]["viewport"]["zoom"] = 3.0
    assert diff(sj.load(base), sj.load(moved)).operations == []


def test_rename_is_one_op_not_drop_add(base):
    renamed = copy.deepcopy(base)
    renamed["logical"]["tables"][0]["fields"][2]["name"] = "display_name"
    ops = diff(sj.load(base), sj.load(renamed)).operations
    names = [o.op for o in ops]
    assert names == ["rename_column"]
    assert ops[0].from_ == "full_name" and ops[0].to == "display_name"


def test_rename_table(base):
    renamed = copy.deepcopy(base)
    renamed["logical"]["tables"][0]["name"] = "accounts"
    ops = diff(sj.load(base), sj.load(renamed)).operations
    assert [o.op for o in ops] == ["rename_table"]


def test_add_table_before_relation(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"].append(
        {"id": "tbl_items0001", "name": "items",
         "fields": [{"id": "fld_it000001", "name": "id", "semanticType": "uuid", "isPrimaryKey": True}]}
    )
    after["logical"]["relations"].append(
        {"id": "rel_oi000001", "type": "one_to_many", "fromTableId": "tbl_items0001", "toTableId": TBL_ORDERS}
    )
    ops = [o.op for o in diff(sj.load(base), sj.load(after)).operations]
    assert ops.index("add_table") < ops.index("add_relation")


def test_drop_relation_before_drop_table(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"] = [after["logical"]["tables"][0]]  # drop orders
    after["logical"]["relations"] = []
    after["physical"] = {"indexes": after["physical"]["indexes"]}
    after["semantic"] = {}
    ops = [o.op for o in diff(sj.load(base, validate=False), sj.load(after, validate=False)).operations]
    # dropping a whole table implies its columns; we emit a single drop_table, ordered last.
    assert ops.index("drop_relation") < ops.index("drop_table")
    assert ops[-1] == "drop_table"


def test_change_type_emits_typed_from_to(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"][1]["fields"][2]["semanticType"] = "string"  # money -> string
    ops = diff(sj.load(base), sj.load(after)).operations
    by = {o.op: o for o in ops}
    assert "change_semantic_type" in by and "change_type" in by
    assert by["change_type"].from_ == "numeric(12,2)" and by["change_type"].to == "varchar(255)"
    assert by["change_type"].field_id == FLD_O_TOTAL


def test_set_and_drop_not_null(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"][0]["fields"][2]["nullable"] = False  # full_name was nullable
    ops = [o.op for o in diff(sj.load(base), sj.load(after)).operations]
    assert "set_not_null" in ops


def test_drop_state_flags_data_loss_note(base):
    after = copy.deepcopy(base)
    sm = after["semantic"]["stateMachines"][0]
    sm["states"] = [s for s in sm["states"] if s["name"] != "cancelled"]
    sm["transitions"] = [t for t in sm["transitions"] if t["name"] != "cancel"]
    ops = diff(sj.load(base, validate=False), sj.load(after, validate=False)).operations
    drop_states = [o for o in ops if o.op == "drop_state"]
    assert drop_states and "data-loss" in drop_states[0].details["note"]


def test_enum_value_add_and_drop(base):
    after = copy.deepcopy(base)
    after["logical"]["enums"][0]["values"] = [{"value": "high"}, {"value": "urgent"}]  # drop low, add urgent
    ops = {o.op for o in diff(sj.load(base), sj.load(after)).operations}
    assert {"add_enum_value", "drop_enum_value"} <= ops


def test_stats_and_changelog_and_colors(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"][0]["fields"][2]["name"] = "display_name"
    after["logical"]["tables"][0]["fields"].append(
        {"id": "fld_new000001", "name": "bio", "semanticType": "text"}
    )
    result = diff(sj.load(base), sj.load(after))
    assert result.stats["renamed"] == 1 and result.stats["added"] == 1
    assert len(result.changelog) == len(result.operations) == len(result.colored)
    assert {c["color"] for c in result.colored} <= {"green", "red", "yellow", "blue"}


def test_op_dicts_feed_risk_contract(base):
    after = copy.deepcopy(base)
    after["logical"]["tables"][0]["fields"] = [
        f for f in after["logical"]["tables"][0]["fields"] if f["name"] != "full_name"
    ]
    op_dicts = diff(sj.load(base, validate=False), sj.load(after, validate=False)).op_dicts()
    drop = next(o for o in op_dicts if o["op"] == "drop_column")
    assert "fieldId" in drop and "tableId" in drop  # camelCase, matches Risk Analyzer input


# --- three-way ------------------------------------------------------------------------------------


def test_three_way_conflict_on_same_attribute(base):
    a = copy.deepcopy(base)
    a["logical"]["tables"][0]["fields"][2]["name"] = "name_a"
    b = copy.deepcopy(base)
    b["logical"]["tables"][0]["fields"][2]["name"] = "name_b"
    result = three_way_diff(sj.load(base), sj.load(a), sj.load(b))
    assert result.has_conflicts
    assert result.conflicts[0].attribute == "name" and result.conflicts[0].a == "name_a"


def test_three_way_no_conflict_when_disjoint(base):
    a = copy.deepcopy(base)
    a["logical"]["tables"][0]["fields"][2]["name"] = "name_a"
    b = copy.deepcopy(base)
    b["logical"]["tables"][1]["fields"][2]["nullable"] = True  # different entity
    result = three_way_diff(sj.load(base), sj.load(a), sj.load(b))
    assert not result.has_conflicts
    assert result.auto_apply_a and result.auto_apply_b


def test_three_way_same_value_is_not_a_conflict(base):
    a = copy.deepcopy(base)
    a["logical"]["tables"][0]["fields"][2]["name"] = "same"
    b = copy.deepcopy(base)
    b["logical"]["tables"][0]["fields"][2]["name"] = "same"
    assert not three_way_diff(sj.load(base), sj.load(a), sj.load(b)).has_conflicts
