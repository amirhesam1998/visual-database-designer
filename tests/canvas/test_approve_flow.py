"""Canvas Milestone 3 (diff + approve) — the gate the canvas drives is the real engine gate.

The canvas computes no diff and decides no approval (spec §0): it shows ``/core/diff`` + ``/core/risk``
and drives the existing approval gate through the session endpoints (a brownfield session whose
baseline IS the canvas base). These tests pin the contract the front-end depends on — including the
hard gate boundary: a CRITICAL migration (dropping a table) is refused until ``acknowledgeCritical``
is set (spec §3/§5 negative test), and a schema with validation errors can't even reach approval.
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.module import app

client = TestClient(app)


def _baseline() -> dict:
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "defaultDriver": "postgres"},
        "logical": {"tables": [
            {"id": "tbl_users0001", "name": "users", "fields": [
                {"id": "fld_uid000001", "name": "id", "semanticType": "uuid", "isPrimaryKey": True}]},
            {"id": "tbl_orders001", "name": "orders", "fields": [
                {"id": "fld_oid000001", "name": "id", "semanticType": "uuid", "isPrimaryKey": True}]},
        ]},
    }


def _without_orders() -> dict:
    """The working doc after the user deletes the orders table on the canvas (→ a drop_table)."""
    doc = copy.deepcopy(_baseline())
    doc["logical"]["tables"] = [t for t in doc["logical"]["tables"] if t["id"] != "tbl_orders001"]
    return doc


def _approve_flow(baseline: dict, working: dict, *, acknowledge: bool):
    """Replicate exactly what the canvas does: brownfield session → apply → validate → submit → approve."""
    sid = client.post("/design/sessions", json={"mode": "brownfield", "schema_json": baseline}).json()["sessionId"]
    client.post(f"/design/sessions/{sid}/apply-suggestion", json={"schema_json": working})
    validated = client.post(f"/design/sessions/{sid}/validate").json()
    submit = client.post(f"/design/sessions/{sid}/submit")
    approve = client.post(
        f"/design/sessions/{sid}/approve",
        json={"approvedBy": "canvas-user", "acknowledgeCritical": acknowledge},
    )
    return validated, submit, approve


def test_diff_shows_drop_and_ignores_table_moves() -> None:
    base = _baseline()
    moved = copy.deepcopy(base)
    moved["presentation"] = {"nodes": [{"tableId": "tbl_users0001", "x": 999, "y": 999}]}
    # a pure move is no schema change
    assert client.post("/core/diff", json={"from": base, "to": moved}).json()["operations"] == []
    # deleting a table is a real, red, drop_table operation
    d = client.post("/core/diff", json={"from": base, "to": _without_orders()}).json()
    assert [o["op"] for o in d["operations"]] == ["drop_table"]
    assert d["colored"][0]["color"] == "red"


def test_drop_table_is_critical_in_risk() -> None:
    r = client.post("/core/risk", json={"from": _baseline(), "to": _without_orders()}).json()
    assert r["max_level"] == "critical"


def test_critical_drop_blocked_without_acknowledgement() -> None:
    validated, submit, approve = _approve_flow(_baseline(), _without_orders(), acknowledge=False)
    assert validated["state"] == "validated"
    assert submit.status_code == 200
    assert approve.status_code == 409
    body = approve.json()
    assert body["reason"] == "critical_migration_risk"
    assert body["blocking"][0]["op"] == "drop_table"


def test_critical_drop_approved_with_acknowledgement() -> None:
    _validated, _submit, approve = _approve_flow(_baseline(), _without_orders(), acknowledge=True)
    assert approve.status_code == 200
    body = approve.json()
    assert body["state"] == "approved"
    assert body["schemaVersion"] == "v1"
    assert body["checksum"].startswith("sha256:")


def test_validation_error_cannot_reach_approval() -> None:
    # working doc has a relation pointing at a non-existent table → a referential validation error.
    broken = copy.deepcopy(_baseline())
    broken["logical"]["relations"] = [
        {"id": "rel_bad00001", "type": "one_to_many", "fromTableId": "tbl_users0001",
         "toTableId": "tbl_ghost0001", "foreignKeyFieldId": "fld_uid000001"},
    ]
    sid = client.post("/design/sessions", json={"mode": "brownfield", "schema_json": _baseline()}).json()["sessionId"]
    client.post(f"/design/sessions/{sid}/apply-suggestion", json={"schema_json": broken})
    validated = client.post(f"/design/sessions/{sid}/validate").json()
    assert validated["state"] != "validated"  # gate refuses to advance past a validation error
    # …so submit (and therefore approve) is impossible.
    assert client.post(f"/design/sessions/{sid}/submit").status_code == 409
