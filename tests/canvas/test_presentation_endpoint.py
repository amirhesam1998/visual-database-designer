"""Canvas Milestone 2 (editing) — presentation save is layout-only, never a schema change.

Moving a table on the canvas updates the ``presentation`` layer (spec M2 §4). The hard guarantee is
that this is *not* a schema edit: the diff engine ignores ``presentation``, so a move must produce an
empty diff and (for a session) must NOT drop the session back to draft or invalidate it. The thin
``POST /design/presentation`` endpoint is the only presentation write the spec permits (§8).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import diff as core_diff
from app.core import schema_json as core_sj
from app.module import app

client = TestClient(app)


def _schema() -> dict:
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                ]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key",
                     "nullable": False},
                ]},
            ],
            "relations": [
                {"id": "rel_order_usr", "type": "one_to_many", "fromTableId": "tbl_orders001",
                 "toTableId": "tbl_users0001", "foreignKeyFieldId": "fld_ouser0001"},
            ],
        },
    }


def test_presentation_stateless_merges_layout_and_echoes_schema() -> None:
    nodes = [
        {"tableId": "tbl_users0001", "x": 40, "y": 80},
        {"tableId": "tbl_orders001", "x": 400, "y": 80},
    ]
    res = client.post("/design/presentation", json={"schema_json": _schema(), "nodes": nodes})
    assert res.status_code == 200
    body = res.json()
    assert body["persisted"] is False
    assert body["schema_json"]["presentation"]["nodes"] == nodes


def test_presentation_move_is_not_a_schema_change() -> None:
    before = _schema()
    after = client.post(
        "/design/presentation",
        json={"schema_json": before, "nodes": [{"tableId": "tbl_users0001", "x": 999, "y": 999}]},
    ).json()["schema_json"]

    ops = core_diff.diff(
        core_sj.load(before, validate=False), core_sj.load(after, validate=False)
    ).op_dicts()
    assert ops == []  # the engine treats a table move as no schema change at all


def test_presentation_persists_to_session_without_changing_state() -> None:
    created = client.post("/design/sessions", json={"schema_json": _schema()}).json()
    sid = created["sessionId"]
    assert created["state"] == "draft"

    res = client.post(
        "/design/presentation",
        json={"sessionId": sid, "nodes": [{"tableId": "tbl_users0001", "x": 7, "y": 7}]},
    )
    assert res.status_code == 200
    assert res.json()["persisted"] is True

    session = client.get(f"/design/sessions/{sid}").json()
    assert session["state"] == "draft"  # layout save never advances/regresses the gate
    assert session["schema_json"]["presentation"]["nodes"] == [{"tableId": "tbl_users0001", "x": 7, "y": 7}]


def test_presentation_requires_nodes() -> None:
    res = client.post("/design/presentation", json={"schema_json": _schema()})
    assert res.status_code == 400


def test_presentation_unknown_session_is_404() -> None:
    res = client.post("/design/presentation", json={"sessionId": "ses_missing", "nodes": []})
    assert res.status_code == 404
