"""Conformance kit — Validation Engine.

Valid fixture passes clean (no errors); each invalid fixture trips the *expected* rule id; the
``vdb-ignore`` mechanism suppresses; and the report serialises to well-formed SARIF.
"""

from __future__ import annotations

from app.core import schema_json as sj
from app.core.validation import Severity, validate

from .factory import (
    FLD_O_USER,
    canonical_schema,
)


def _ids(report):
    return {f.rule_id for f in report.findings}


def test_canonical_schema_has_no_errors():
    report = validate(sj.load(canonical_schema()))
    assert report.valid
    assert report.summary["error"] == 0


# --- referential integrity ------------------------------------------------------------------------


def test_relation_to_missing_table():
    doc = canonical_schema()
    doc["logical"]["relations"][0]["toTableId"] = "tbl_ghost0001"
    report = validate(sj.load(doc, validate=False))
    assert "REF002" in _ids(report) and not report.valid


def test_index_column_not_in_table():
    doc = canonical_schema()
    doc["physical"]["indexes"][0]["columns"] = [FLD_O_USER]  # belongs to orders, not users
    report = validate(sj.load(doc, validate=False))
    assert "REF011" in _ids(report)


def test_field_enum_id_missing():
    doc = canonical_schema()
    doc["logical"]["tables"][1]["fields"][3]["enumId"] = "enm_missing01"
    report = validate(sj.load(doc, validate=False))
    assert "REF020" in _ids(report)


# --- state machine rules --------------------------------------------------------------------------


def test_two_initial_states_error():
    doc = canonical_schema()
    doc["semantic"]["stateMachines"][0]["states"][1]["initial"] = True  # now two initials
    report = validate(sj.load(doc, validate=False))
    assert "SM003" in _ids(report)


def test_transition_to_undefined_state():
    doc = canonical_schema()
    doc["semantic"]["stateMachines"][0]["transitions"][0]["to"] = "stt_ghost0001"
    report = validate(sj.load(doc, validate=False))
    assert "SM006" in _ids(report)


def test_unreachable_state_and_deadlock():
    doc = canonical_schema()
    sm = doc["semantic"]["stateMachines"][0]
    # add an island state nobody transitions to and which has no outgoing transition
    sm["states"].append({"id": "stt_island01", "name": "island"})
    report = validate(sj.load(doc, validate=False))
    assert "SM008" in _ids(report)  # unreachable
    assert "SM009" in _ids(report)  # deadlock


def test_state_machine_on_non_status_field():
    doc = canonical_schema()
    # point the machine at the (uuid) id field instead of the status field
    doc["semantic"]["stateMachines"][0]["fieldId"] = doc["logical"]["tables"][1]["fields"][0]["id"]
    report = validate(sj.load(doc, validate=False))
    assert "SM002" in _ids(report)


# --- quality / security / performance -------------------------------------------------------------


def test_table_without_primary_key():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"][0]["isPrimaryKey"] = False
    report = validate(sj.load(doc, validate=False))
    assert "QLT001" in _ids(report)


def test_duplicate_field_name():
    doc = canonical_schema()
    fields = doc["logical"]["tables"][0]["fields"]
    fields.append({"id": "fld_dup000001", "name": "email", "semanticType": "string"})
    report = validate(sj.load(doc, validate=False))
    assert "QLT002" in _ids(report) and not report.valid


def test_email_without_unique_index_warns():
    doc = canonical_schema()
    doc["physical"]["indexes"] = []  # drop the unique email index
    report = validate(sj.load(doc, validate=False))
    assert "QLT010" in _ids(report)


def test_money_as_float_warns():
    doc = canonical_schema()
    # override the orders.total physical type to a float
    doc["logical"]["tables"][1]["fields"][2]["overrides"] = {"physical": {"type": "float"}}
    report = validate(sj.load(doc, validate=False))
    assert "QLT011" in _ids(report)


def test_fk_not_indexed_is_performance_note():
    report = validate(sj.load(canonical_schema()))
    perf = [f for f in report.findings if f.severity == Severity.PERFORMANCE]
    assert any(f.rule_id == "PRF001" for f in perf)


# --- ignore mechanism -----------------------------------------------------------------------------


def test_global_ignore_suppresses():
    report = validate(sj.load(canonical_schema()), ignore={"PRF001"})
    assert "PRF001" not in _ids(report)
    assert report.suppressed >= 1


def test_inline_comment_ignore_suppresses():
    doc = canonical_schema()
    # suppress PRF001 only on the orders.user_id field via an inline comment
    doc["logical"]["tables"][1]["fields"][1]["comment"] = "vdb-ignore: PRF001 indexed elsewhere"
    report = validate(sj.load(doc))
    assert "PRF001" not in _ids(report)
    assert report.suppressed >= 1


def test_scoped_ignore_does_not_leak_to_other_entities():
    doc = canonical_schema()
    # add a second un-indexed FK so two PRF001 would fire; ignore only one entity
    doc["logical"]["tables"][0]["fields"].append(
        {"id": "fld_owner0001", "name": "owner_id", "semanticType": "foreign_key"}
    )
    doc["logical"]["relations"].append(
        {"id": "rel_self000001", "type": "one_to_many", "fromTableId": doc["logical"]["tables"][0]["id"],
         "toTableId": doc["logical"]["tables"][0]["id"], "foreignKeyFieldId": "fld_owner0001"}
    )
    report = validate(sj.load(doc, validate=False), ignore={FLD_O_USER: {"PRF001"}})
    perf = [f for f in report.findings if f.rule_id == "PRF001"]
    assert any(f.entity_id == "fld_owner0001" for f in perf)
    assert all(f.entity_id != FLD_O_USER for f in perf)


# --- SARIF ----------------------------------------------------------------------------------------


def test_sarif_shape():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"][0]["isPrimaryKey"] = False
    sarif = validate(sj.load(doc, validate=False)).to_sarif()
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "vdb-validate"
    assert all("ruleId" in r and r["level"] in {"error", "warning", "note"} for r in run["results"])
