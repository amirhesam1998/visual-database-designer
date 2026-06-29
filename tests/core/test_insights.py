"""Conformance kit — Insights Engine (intelligence milestone).

The spec's axis is the fact/suggestion split (§0): certain structural facts (no PK, FK type mismatch)
are reported with confidence; heuristic guesses (a sensitive-looking column name) are carried as
suggestions to confirm. Every insight has a severity and a *why* (§6), the analysis is deterministic
and LLM-free (same schema → same insights), and an applicable finding carries a structured action
that maps onto a normal structural edit (§5).
"""

from __future__ import annotations

from app.core import insights as ins
from app.core import schema_json as sj
from app.core.type_system import PhysicalSpec, SemanticTypeDef, build_default_registry

from .factory import (
    FLD_O_USER,
    FLD_U_EMAIL,
    TBL_ORDERS,
    TBL_USERS,
    canonical_schema,
)


def _ids(report: ins.InsightReport) -> set[str]:
    return {i.rule_id for i in report.insights}


def _by_id(report: ins.InsightReport, rule_id: str) -> ins.Insight:
    return next(i for i in report.insights if i.rule_id == rule_id)


# --- determinism + the core contract --------------------------------------------------------------


def test_every_insight_has_a_why_and_a_severity():
    report = ins.analyze(sj.load(canonical_schema()))
    assert report.insights  # the canonical schema has at least the FK-index suggestion
    for i in report.insights:
        assert i.why.strip(), f"{i.rule_id} has no 'why'"
        assert i.severity in ins.InsightSeverity
        assert i.kind in ins.InsightKind


def test_analysis_is_deterministic():
    doc = canonical_schema()
    a = ins.analyze(sj.load(doc)).model_dump()
    b = ins.analyze(sj.load(doc)).model_dump()
    assert a == b


# --- §1 Index Advisor -----------------------------------------------------------------------------


def test_foreign_key_without_index_is_a_suggestion_with_an_add_index_action():
    # The canonical orders.user_id FK has no index → IDX001.
    report = ins.analyze(sj.load(canonical_schema()))
    assert "IDX001" in _ids(report)
    fk = _by_id(report, "IDX001")
    assert fk.kind == ins.InsightKind.SUGGESTION
    assert fk.action and fk.action.type == "add_index"
    assert fk.action.columns == [FLD_O_USER] and fk.action.unique is False


def test_adding_the_index_resolves_the_suggestion():
    doc = canonical_schema()
    doc["physical"]["indexes"].append(
        {"id": "idx_ouser0001", "tableId": TBL_ORDERS, "columns": [FLD_O_USER], "unique": False}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    assert "IDX001" not in _ids(report)  # the FK is now covered


def test_redundant_index_is_a_fact():
    doc = canonical_schema()
    doc["physical"]["indexes"].append(
        {"id": "idx_uemail002", "tableId": TBL_USERS, "columns": [FLD_U_EMAIL], "unique": True}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    assert "IDX010" in _ids(report)
    assert _by_id(report, "IDX010").kind == ins.InsightKind.FACT


# --- §2 Design Warnings (facts) -------------------------------------------------------------------


def test_table_without_primary_key():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"][0]["isPrimaryKey"] = False  # users loses its PK
    report = ins.analyze(sj.load(doc, validate=False))
    assert "DSN001" in _ids(report)
    assert _by_id(report, "DSN001").kind == ins.InsightKind.FACT


def test_concrete_fk_type_mismatch_is_flagged_but_generic_foreign_key_is_not():
    # The generic 'foreign_key' type inherits the PK's type — NOT a mismatch.
    assert "DSN003" not in _ids(ins.analyze(sj.load(canonical_schema())))
    # An explicit integer FK onto a uuid PK is the classic, real mismatch.
    doc = canonical_schema()
    doc["logical"]["tables"][1]["fields"][1]["semanticType"] = "integer"
    report = ins.analyze(sj.load(doc, validate=False))
    assert "DSN003" in _ids(report)
    mm = _by_id(report, "DSN003")
    assert mm.kind == ins.InsightKind.FACT and "uuid" in mm.why


def test_relation_without_on_delete():
    doc = canonical_schema()
    doc["logical"]["relations"][0].pop("onDelete")
    report = ins.analyze(sj.load(doc, validate=False))
    assert "DSN004" in _ids(report)


def test_island_table_is_a_warning_not_an_error():
    doc = canonical_schema()
    doc["logical"]["tables"].append(
        {"id": "tbl_island001", "name": "settings", "kind": "normal",
         "fields": [{"id": "fld_sid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False}]}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    island = next(i for i in report.insights if i.rule_id == "DSN005" and i.table_id == "tbl_island001")
    assert island.severity == ins.InsightSeverity.WARNING


def test_inconsistent_naming_is_a_suggestion():
    doc = canonical_schema()  # all snake_case so far
    doc["logical"]["tables"][0]["fields"].append(
        {"id": "fld_ucreated1", "name": "createdAt", "semanticType": "datetime", "nullable": True}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    assert "DSN006" in _ids(report)
    assert _by_id(report, "DSN006").kind == ins.InsightKind.SUGGESTION


def test_varchar_without_length():
    # No built-in type resolves to a length-less varchar (the Type System always assigns one), so we
    # register one to exercise the rule — exactly the shape an odd imported column could take.
    reg = build_default_registry()
    reg.register(SemanticTypeDef(id="bare_str", category="string",
                                 physical_default=PhysicalSpec(type="varchar")))
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"].append(
        {"id": "fld_ubio00001", "name": "bio", "semanticType": "bare_str", "nullable": True}
    )
    report = ins.analyze(sj.load(doc, validate=False), registry=reg)
    assert "DSN002" in _ids(report)


# --- §3 Sensitive Field Detection -----------------------------------------------------------------


def test_pii_typed_field_is_a_certain_fact():
    report = ins.analyze(sj.load(canonical_schema()))
    pii = [i for i in report.insights if i.rule_id == "PRV001"]
    assert any(i.field_id == FLD_U_EMAIL for i in pii)  # email is PII by its type
    assert all(i.kind == ins.InsightKind.FACT for i in pii)


def test_suspicious_name_on_a_generic_column_is_a_suggestion_with_an_action():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"].append(
        {"id": "fld_uphone001", "name": "phone_number", "semanticType": "string", "nullable": True}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    assert "PRV002" in _ids(report)
    sug = _by_id(report, "PRV002")
    assert sug.kind == ins.InsightKind.SUGGESTION
    assert sug.action and sug.action.type == "mark_sensitive" and sug.action.field_id == "fld_uphone001"


def test_marking_the_field_sensitive_turns_the_suggestion_into_a_fact():
    # Applying the action (a privacy override) is what the canvas does — re-analysis then reports it as
    # a certain PII fact, not a guess (the apply→re-derive loop, spec §5).
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"].append(
        {"id": "fld_uphone001", "name": "phone_number", "semanticType": "string", "nullable": True,
         "overrides": {"privacy": {"pii": True, "sensitivity": "medium"}}}
    )
    report = ins.analyze(sj.load(doc, validate=False))
    assert "PRV002" not in _ids(report)
    assert any(i.rule_id == "PRV001" and i.field_id == "fld_uphone001" for i in report.insights)
