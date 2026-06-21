"""Canvas Milestone 1 (read-only) — proof the canvas talks to the *real* engine.

The canvas itself is a front-end (tested with Vitest under ``frontend-canvas/``), but its data
contract is the read-only ``POST /design/render`` projection. The acceptance criterion that matters
here (spec §7) is that the canvas never re-derives database logic: the render endpoint reuses the
Type System's existing resolution, so a foreign-key column shows its *referenced primary key's*
physical type — a uuid FK renders as ``uuid``, never an integer (the lesson of the whole project).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.module import app

client = TestClient(app)


def _schema() -> dict:
    """orders.user_id is a FK (semanticType ``foreign_key``) onto users.id (a uuid PK)."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                    {"id": "fld_upass0001", "name": "password", "semanticType": "password", "nullable": False},
                ]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False},
                ]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
        "presentation": {"nodes": [
            {"tableId": "tbl_users0001", "x": 40, "y": 80},
            {"tableId": "tbl_orders001", "x": 400, "y": 80},
        ]},
    }


def test_render_returns_tables_relations_and_layout() -> None:
    res = client.post("/design/render", json={"schema_json": _schema()})
    assert res.status_code == 200
    body = res.json()

    names = {t["name"] for t in body["tables"]}
    assert names == {"users", "orders"}

    assert len(body["relations"]) == 1
    rel = body["relations"][0]
    assert rel["type"] == "one_to_many"
    assert rel["fromTableId"] == "tbl_orders001"
    assert rel["toTableId"] == "tbl_users0001"

    # presentation positions survive → the canvas uses them instead of auto-layout
    assert body["hasLayout"] is True
    assert len(body["presentation"]["nodes"]) == 2


def test_fk_column_inherits_referenced_pk_physical_type() -> None:
    body = client.post("/design/render", json={"schema_json": _schema()}).json()
    orders = next(t for t in body["tables"] if t["name"] == "orders")
    user_id = next(f for f in orders["fields"] if f["name"] == "user_id")

    # The FK column resolves to the referenced uuid PK's physical type, not an integer.
    assert user_id["isForeignKey"] is True
    assert user_id["physicalType"] == "uuid"


def test_sensitive_field_is_flagged_pii() -> None:
    body = client.post("/design/render", json={"schema_json": _schema()}).json()
    users = next(t for t in body["tables"] if t["name"] == "users")
    password = next(f for f in users["fields"] if f["name"] == "password")
    email = next(f for f in users["fields"] if f["name"] == "email")
    assert password["pii"] is True
    assert email["pii"] is True


def test_render_via_session_id() -> None:
    created = client.post("/design/sessions", json={"schema_json": _schema()}).json()
    sid = created["sessionId"]
    body = client.post("/design/render", json={"sessionId": sid}).json()
    assert {t["name"] for t in body["tables"]} == {"users", "orders"}


def test_render_no_layout_signals_auto_layout() -> None:
    schema = _schema()
    schema.pop("presentation")
    body = client.post("/design/render", json={"schema_json": schema}).json()
    assert body["hasLayout"] is False
    assert body["presentation"]["nodes"] == []


def test_render_rejects_garbage() -> None:
    res = client.post("/design/render", json={"schema_json": {"logical": {"tables": "nope"}}})
    assert res.status_code == 400
