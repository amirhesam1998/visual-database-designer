"""Conformance kit — Milestone 4: API Contract (OpenAPI as Source of Truth) (spec §8/§9).

The acceptance gate (marked ``conformance``). The headline is the **full loop at the HTTP layer**:
``schema → migration (M1) → apply → seed (M3) → start the generated API → real HTTP requests`` against
the same Postgres — proving both the contract *and* the generated server. A snapshot and OpenAPI
validation are not enough; the live-Postgres test is opt-in but **must pass once** to count as proven
(the lesson M1/M2/M3 keep teaching).

Beyond the positive loop it proves, at the HTTP level: the FK-type lesson (numeric ``user_id`` → 422
before the DB; a valid-but-nonexistent uuid → 409 from the DB's FK constraint), state-machine
enforcement on ``PATCH`` (legal transition → 200, illegal → 422), RFC 7807 validation, determinism of
the OpenAPI document and the server files, and the no-LLM guarantee.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.core import schema_json as sj
from app.core.api_contract import build_contract, build_openapi
from app.core.api_server import generate_server_files
from app.core.diff import diff
from app.core.seeder import seed_data
from app.core.sql_emitter import emit_sql

pytestmark = pytest.mark.conformance

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _ecommerce_schema() -> dict:
    """The M1/M2/M3 reference schema: uuid PK, a uuid FK, an enum-backed status state machine."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False}]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_ostatus01", "name": "status", "semanticType": "status", "nullable": False},
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
        "semantic": {"stateMachines": [{
            "id": "sm_order_status", "name": "OrderStatus", "fieldId": "fld_ostatus01",
            "states": [{"id": "stt_pending", "name": "pending", "initial": True},
                       {"id": "stt_delivered", "name": "delivered"},
                       {"id": "stt_cancelled", "name": "cancelled", "final": True}],
            "transitions": [{"id": "trn_deliver", "name": "deliver", "from": "stt_pending", "to": "stt_delivered"},
                            {"id": "trn_cancel", "name": "cancel", "from": "stt_pending", "to": "stt_cancelled"}],
        }]},
    }


_SCENARIO = {"name": "ecommerce_small", "params": {"users": 3, "orders": 8}}


# --- offline acceptance (snapshots + determinism + no-LLM) --------------------------------------


def test_openapi_snapshot_is_source_of_truth():
    """The OpenAPI 3.1 document is the headline artifact: stable shape, FK uuid, RFC 7807."""
    openapi = build_openapi(_ecommerce_schema())
    assert openapi["openapi"] == "3.1.0"
    assert "/v1/orders" in openapi["paths"] and "/v1/orders/{id}" in openapi["paths"]
    create = openapi["components"]["schemas"]["OrderCreate"]
    assert create["properties"]["user_id"]["format"] == "uuid"   # FK exposed as uuid, not int
    assert "id" not in create["properties"]                       # read-only PK absent from input
    assert "Problem" in openapi["components"]["schemas"]          # RFC 7807


def test_openapi_and_server_files_are_byte_identical_across_runs():
    """Determinism (spec §8): same schema → byte-identical OpenAPI + server files (the ordering trap)."""
    o1 = json.dumps(build_openapi(_ecommerce_schema()), sort_keys=True)
    o2 = json.dumps(build_openapi(_ecommerce_schema()), sort_keys=True)
    assert o1 == o2
    assert generate_server_files(_ecommerce_schema()) == generate_server_files(_ecommerce_schema())


def test_no_llm_path_is_complete():
    """The whole pipeline runs without any LLM (none is passed): contract → OpenAPI → server."""
    contract = build_contract(_ecommerce_schema())
    openapi = build_openapi(_ecommerce_schema(), contract=contract)
    files = generate_server_files(_ecommerce_schema(), contract=contract)
    assert openapi["paths"] and files["main.py"]


# --- live PostgreSQL (opt-in, but must pass once to count as proven — spec §8 "live gate") ------
def _driver_or_skip() -> str:
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN to run the live-Postgres API tests")
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg not installed")
    return dsn


def _reset(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS "orders" CASCADE; DROP TABLE IF EXISTS "users" CASCADE;')


def _executable(statements: list[str]) -> list[str]:
    return [s for s in statements if not s.strip().startswith("--")]


def _build_app(schema_dict: dict):
    """``exec`` the *generated* main.py so the live gate drives exactly the emitted artifact."""
    files = generate_server_files(schema_dict, version="v1")
    namespace: dict = {"__name__": "generated_main"}
    exec(compile(files["main.py"], "generated_main.py", "exec"), namespace)  # noqa: S102 - generated artifact
    return namespace["app"]


def _apply_schema_and_seed(dsn: str, schema_dict: dict) -> dict:
    import psycopg

    target = sj.load(schema_dict)
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    migration = _executable(emit_sql(diff(empty, target).op_dicts(), target).up_statements())
    seed = seed_data(schema_dict, seed=42, scenario=_SCENARIO, output="sql")
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in migration:
                cur.execute(stmt)
            for stmt in seed["sql"]["statements"]:
                cur.execute(stmt)
    return seed


@pytest.mark.live_postgres
def test_full_loop_schema_migration_seed_api_real_http():
    """Headline acceptance (spec §8): build with M1's emitter, apply, seed with M3, start the generated
    API against the same DB, and drive it with real HTTP — list/get/create, FK-type proof, state
    machine, validation — verifying results both in the response and (for POST) in the database."""
    import psycopg

    dsn = _driver_or_skip()
    _reset(dsn)
    schema_dict = _ecommerce_schema()
    try:
        seed = _apply_schema_and_seed(dsn, schema_dict)
        os.environ["DATABASE_URL"] = dsn
        client = TestClient(_build_app(schema_dict))

        # GET list → the real seeded rows.
        resp = client.get("/v1/orders")
        assert resp.status_code == 200
        orders = resp.json()
        assert len(orders) == seed["rows"]["orders"]

        # GET by id → the right row, with a uuid FK.
        one = orders[0]
        resp = client.get(f"/v1/orders/{one['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == one["id"]
        assert _UUID_RE.match(resp.json()["user_id"])     # FK serialised as a uuid, not an int

        # Grab a real seeded user to satisfy the FK on create.
        resp = client.get("/v1/users")
        real_user_id = resp.json()[0]["id"]
        assert _UUID_RE.match(real_user_id)

        # POST a valid order → 201, and verify it in the DB via a JOIN to the user.
        resp = client.post("/v1/orders",
                          json={"user_id": real_user_id, "status": "pending", "total": 42.5})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT u.email FROM "orders" o JOIN "users" u ON o.user_id = u.id '
                        'WHERE o.id = %s', (new_id,))
            assert cur.fetchone() is not None         # the FK actually resolves on the server

        # FK-type proof (spec §8): a numeric user_id is rejected by validation before the DB → 422.
        resp = client.post("/v1/orders", json={"user_id": 123, "status": "pending", "total": 1})
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert any(e["field"] == "user_id" for e in resp.json()["errors"])

        # A valid-but-nonexistent uuid passes validation, then the DB's FK constraint rejects it → 409.
        resp = client.post("/v1/orders",
                          json={"user_id": "00000000-0000-4000-8000-000000000000",
                                "status": "pending", "total": 1})
        assert resp.status_code == 409

        # State-machine enforcement on PATCH: legal pending → delivered → 200; illegal reverse → 422.
        resp = client.patch(f"/v1/orders/{new_id}", json={"status": "delivered"})
        assert resp.status_code == 200 and resp.json()["status"] == "delivered"
        resp = client.patch(f"/v1/orders/{new_id}", json={"status": "pending"})
        assert resp.status_code == 422
        assert any(e["field"] == "status" for e in resp.json()["errors"])

        # Validation (problem+json, per-field) on a bad email and a missing required field.
        resp = client.post("/v1/users", json={"email": "nope"})
        assert resp.status_code == 422 and any(e["field"] == "email" for e in resp.json()["errors"])
        resp = client.post("/v1/users", json={})
        assert resp.status_code == 422 and any(e["message"] == "required" for e in resp.json()["errors"])

        # Not found → 404 problem+json.
        resp = client.get("/v1/orders/00000000-0000-4000-8000-000000000000")
        assert resp.status_code == 404
    finally:
        os.environ.pop("DATABASE_URL", None)
        _reset(dsn)
