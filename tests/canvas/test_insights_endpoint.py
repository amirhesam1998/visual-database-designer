"""Intelligence milestone — the ``/design/insights`` surface the ``/designer`` UI calls.

The engine owns the analysis (golden rule); this pins the HTTP contract: a schema_json in, a list of
findings out (each with a kind/severity/why), the fact-vs-suggestion split, the structured action a
finding may carry, and a clean 400 for malformed input. The deep rule coverage lives in
:mod:`tests.core.test_insights`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.module import app

client = TestClient(app)


def _schema() -> dict:
    """users(uuid PK, email) ← orders.user_id (FK, no index)."""
    return {
        "formatVersion": "1.0.0",
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                ]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                ]},
            ],
            "relations": [
                {"id": "rel_order_usr", "type": "one_to_many", "fromTableId": "tbl_orders001",
                 "toTableId": "tbl_users0001", "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
    }


def test_insights_endpoint_returns_findings_with_why_and_kind() -> None:
    res = client.post("/design/insights", json={"schema": _schema()})
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body["insights"], list) and body["insights"]
    for i in body["insights"]:
        assert i["why"].strip()
        assert i["kind"] in {"fact", "suggestion"}
        assert i["severity"] in {"error", "warning", "info"}


def test_fk_index_suggestion_carries_an_add_index_action() -> None:
    body = client.post("/design/insights", json={"schema": _schema()}).json()
    idx = next(i for i in body["insights"] if i["rule_id"] == "IDX001")
    assert idx["kind"] == "suggestion"
    assert idx["action"]["type"] == "add_index"
    assert idx["action"]["columns"] == ["fld_ouser0001"]


def test_summary_separates_facts_from_suggestions() -> None:
    body = client.post("/design/insights", json={"schema": _schema()}).json()
    assert "fact" in body["summary"] and "suggestion" in body["summary"]
    kinds = {i["kind"] for i in body["insights"]}
    assert body["summary"]["suggestion"] >= 1
    assert kinds.issubset({"fact", "suggestion"})


def test_malformed_schema_is_a_400_not_a_500() -> None:
    res = client.post("/design/insights", json={"schema": {"logical": {"tables": "not-a-list"}}})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_schema_json"


def test_insights_endpoint_is_advertised_in_capabilities() -> None:
    caps = client.get("/capabilities").json()
    assert "/design/insights" in caps["endpoints"]["insights"]
    assert "/design/insights" in caps["endpoints"]["designer"]
