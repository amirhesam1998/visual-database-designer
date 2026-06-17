"""Integration — the thin /core/* HTTP wrappers expose the deterministic Core (AD-4)."""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.module import app

from .factory import canonical_schema

client = TestClient(app)


def test_core_types_endpoint():
    body = client.get("/core/types").json()
    ids = {t["id"] for t in body["types"]}
    assert {"email", "money", "uuid", "status"} <= ids


def test_core_validate_endpoint():
    body = client.post("/core/validate", json={"schema": canonical_schema(), "sarif": True}).json()
    assert body["structuralErrors"] == []
    assert body["report"]["valid"] is True
    assert body["sarif"]["version"] == "2.1.0"


def test_core_migrate_endpoint():
    v0 = {"tables": [{"id": "tbl_users0001", "name": "users",
                      "fields": [{"id": "fld_uid000001", "name": "id", "semanticType": "uuid"}]}]}
    body = client.post("/core/migrate", json={"schema": v0}).json()
    assert body["formatVersion"] == "1.0.0" and body["structuralErrors"] == []


def test_core_diff_and_risk_endpoints():
    base = canonical_schema()
    after = copy.deepcopy(base)
    after["logical"]["tables"] = [after["logical"]["tables"][0]]  # drop orders → critical
    after["logical"]["relations"] = []
    after["physical"] = {"indexes": after["physical"]["indexes"]}
    after["semantic"] = {}

    diff_body = client.post("/core/diff", json={"from": base, "to": after}).json()
    assert any(o["op"] == "drop_table" for o in diff_body["operations"])

    risk_body = client.post("/core/risk", json={"from": base, "to": after, "sarif": True}).json()
    assert risk_body["max_level"] == "critical" and risk_body["exit_code"] == 2
    assert risk_body["checklist"]


def test_core_three_way_diff_endpoint():
    base = canonical_schema()
    a = copy.deepcopy(base)
    a["logical"]["tables"][0]["fields"][2]["name"] = "name_a"
    b = copy.deepcopy(base)
    b["logical"]["tables"][0]["fields"][2]["name"] = "name_b"
    body = client.post("/core/diff", json={"base": base, "from": a, "to": b}).json()
    assert body["threeWay"]["has_conflicts"] is True


def test_core_state_machine_endpoint():
    body = client.post("/core/state-machine", json={
        "stateMachine": canonical_schema()["semantic"]["stateMachines"][0],
        "schema": canonical_schema(),
    }).json()
    assert body["valid"] is True
    assert body["enum"] == ["pending", "paid", "shipped", "cancelled"]
    assert body["seeder_plan"]["shipped"] == ["pay", "ship"]
