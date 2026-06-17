"""Conformance kit — Migration Risk Analyzer (``docs/spec-migration-risk-analyzer.md`` §11).

Per-op level + safePlan; reversibility/backup flags; driver matrix gives the right clause/lockImpact;
rolling vs downtime produce different plans; SARIF + exit codes.
"""

from __future__ import annotations

import copy

import pytest

from app.core import diff
from app.core import schema_json as sj
from app.core.risk import RiskLevel, analyze

from .factory import canonical_schema


def _one(op: dict, **kw):
    return analyze([op], **kw).operations[0]


# --- per-op risk level + reversibility (spec §3, §6) ----------------------------------------------


@pytest.mark.parametrize(
    ("op", "expected_level"),
    [
        ({"op": "add_table", "tableId": "t"}, "safe"),
        ({"op": "add_column", "tableId": "t", "field": {"nullable": True}}, "safe"),
        ({"op": "add_column", "tableId": "t", "field": {"nullable": False}}, "high"),
        ({"op": "add_column", "tableId": "t", "field": {"nullable": False, "default": "x"}}, "low"),
        ({"op": "drop_column", "fieldId": "f"}, "high"),
        ({"op": "set_not_null", "fieldId": "f"}, "high"),
        ({"op": "drop_not_null", "fieldId": "f"}, "safe"),
        ({"op": "add_index", "entityId": "i", "unique": False}, "medium"),
        ({"op": "add_index", "entityId": "i", "unique": True}, "high"),
        ({"op": "drop_enum_value", "entityId": "e"}, "high"),
        ({"op": "drop_table", "tableId": "t"}, "critical"),
    ],
)
def test_per_op_level(op, expected_level):
    assert _one(op).level == expected_level


def test_irreversible_ops_require_backup():
    for opname in [{"op": "drop_table", "tableId": "t"}, {"op": "drop_column", "fieldId": "f"}]:
        r = _one(opname)
        assert r.reversible is False and r.requires_backup is True


def test_reversible_ops_have_a_rollback():
    r = _one({"op": "add_column", "tableId": "t", "field": {"nullable": True}})
    assert r.reversible and r.rollback == "drop_column"


# --- safe plans (expand/contract) (spec §5) -------------------------------------------------------


def test_set_not_null_has_three_step_plan_and_backfill():
    r = _one({"op": "set_not_null", "fieldId": "f"})
    assert len(r.safe_plan) == 3
    assert r.backfill and r.backfill["idempotent"] is True


def test_change_type_narrowing_vs_widening():
    narrow = _one({"op": "change_type", "fieldId": "f", "from": "varchar(255)", "to": "varchar(50)"})
    wide = _one({"op": "change_type", "fieldId": "f", "from": "varchar(50)", "to": "varchar(255)"})
    assert narrow.level == "high" and narrow.requires_backup and narrow.safe_plan
    assert wide.level == "low" and not wide.requires_backup


def test_incompatible_type_change_is_narrowing():
    r = _one({"op": "change_type", "fieldId": "f", "from": "varchar(255)", "to": "integer"})
    assert r.level == "high"


# --- rolling vs downtime (spec §5.3) --------------------------------------------------------------


def test_rename_rolling_has_plan_downtime_does_not():
    op = {"op": "rename_column", "fieldId": "f", "from": "a", "to": "b"}
    assert _one(op, deploy_mode="rolling").safe_plan
    assert _one(op, deploy_mode="downtime").safe_plan == []


# --- driver matrix (spec §4) ----------------------------------------------------------------------


def test_driver_specific_clauses():
    pg = _one({"op": "add_index", "entityId": "i"}, driver="postgres")
    my = _one({"op": "add_index", "entityId": "i"}, driver="mysql")
    assert "CONCURRENTLY" in pg.recommended_clause
    assert "INPLACE" in my.recommended_clause


def test_set_not_null_pg_recommends_not_valid_validate():
    pg = _one({"op": "set_not_null", "fieldId": "f"}, driver="postgres")
    assert "NOT VALID" in pg.recommended_clause and "VALIDATE" in pg.recommended_clause


# --- report-level: summary, exit codes, gate, SARIF, checklist ------------------------------------


def test_exit_codes_and_gate():
    safe = analyze([{"op": "add_table", "tableId": "t"}])
    assert safe.exit_code == 0 and not safe.gate("critical")

    high = analyze([{"op": "drop_column", "fieldId": "f"}])
    assert high.exit_code == 1 and high.gate("high") and not high.gate("critical")

    crit = analyze([{"op": "drop_table", "tableId": "t"}])
    assert crit.exit_code == 2 and crit.gate("critical")


def test_sarif_and_checklist():
    report = analyze([{"op": "drop_table", "tableId": "tbl_orders001"}])
    sarif = report.to_sarif()
    assert sarif["runs"][0]["results"][0]["level"] == "error"
    assert sarif["runs"][0]["results"][0]["properties"]["requiresBackup"] is True
    assert any("backup" in line.lower() for line in report.checklist())


def test_safe_migration_checklist_when_clean():
    report = analyze([{"op": "add_table", "tableId": "t"}, {"op": "drop_not_null", "fieldId": "f"}])
    assert report.checklist() == ["No destructive operations — safe to apply."]


# --- end-to-end: diff → risk on the canonical schema ----------------------------------------------


def test_diff_to_risk_pipeline():
    base = canonical_schema()
    after = copy.deepcopy(base)
    after["logical"]["tables"] = [after["logical"]["tables"][0]]  # drop the orders table
    after["logical"]["relations"] = []
    after["physical"] = {"indexes": after["physical"]["indexes"]}
    after["semantic"] = {}
    op_dicts = diff.diff(sj.load(base, validate=False), sj.load(after, validate=False)).op_dicts()
    report = analyze(op_dicts, driver="postgres")
    assert report.max_level == "critical"  # dropping orders
    assert report.gate("critical")
    drop = next(o for o in report.operations if o.op == "drop_table")
    assert drop.requires_backup and not drop.reversible


def test_max_level_ordering_uses_intenum():
    assert RiskLevel.from_label("critical") > RiskLevel.from_label("high") > RiskLevel.from_label("safe")
