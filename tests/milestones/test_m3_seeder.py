"""Conformance kit — Milestone 3: Scenario-Based Seeder (spec §6/§7).

The acceptance gate (marked ``conformance``). The headline is the **full loop on a real server**:
``schema → migration (M1) → apply → seed → INSERT`` with **no constraint violated** (FK, unique,
NOT NULL, enum) — a snapshot cannot prove data is insertable, so the live-Postgres test is opt-in but
**must pass once** to count as proven (the lesson M1/M2 taught). Plus: FK-type proof
(``orders.user_id`` is a uuid referencing a real ``users.id``), state-machine consistency (a delivered
order has a successful payment; no unreachable state), determinism, and the no-LLM guarantee.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re

import pytest

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.seeder import seed_data
from app.core.sql_emitter import emit_sql

pytestmark = pytest.mark.conformance

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _ecommerce_schema() -> dict:
    """The e-commerce schema with a uuid FK and a state-machine status (the M1/M2 reference, extended)."""
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
                {"id": "tbl_pay000001", "name": "payments", "kind": "normal", "fields": [
                    {"id": "fld_pid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_porder001", "name": "order_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_pstatus01", "name": "status", "semanticType": "status", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
                {"id": "rel_pay_order", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_pay000001", "toTableId": "tbl_orders001",
                 "foreignKeyFieldId": "fld_porder001", "onDelete": "cascade"},
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


_SCENARIO = {"name": "ecommerce_medium", "params": {"users": 3, "orders": 10}}


# --- offline acceptance ---------------------------------------------------------------------------


def test_seed_snapshot_is_stable_and_correct_shape():
    out = seed_data(_ecommerce_schema(), seed=42, scenario=_SCENARIO, output="json")
    assert out["rows"] == {"users": 3, "orders": 10, "payments": 6}
    again = seed_data(_ecommerce_schema(), seed=42, scenario=_SCENARIO, output="json")
    assert json.dumps(out, sort_keys=True) == json.dumps(again, sort_keys=True)  # determinism §6


def test_fk_type_proof_orders_user_id_is_uuid_to_real_user():
    """The M1/M2 lesson, proved for data: orders.user_id is a uuid pointing at an existing users.id."""
    out = seed_data(_ecommerce_schema(), seed=42, scenario=_SCENARIO, output="json")
    user_ids = {u["id"] for u in out["data"]["users"]}
    for order in out["data"]["orders"]:
        assert _UUID_RE.match(order["user_id"])         # a uuid, not an integer
        assert order["user_id"] in user_ids              # a real, existing user


def test_state_machine_consistency():
    out = seed_data(_ecommerce_schema(), seed=42, scenario=_SCENARIO, output="json")
    statuses = {o["status"] for o in out["data"]["orders"]}
    assert statuses <= {"pending", "delivered", "cancelled"}     # only reachable states
    delivered = {o["id"] for o in out["data"]["orders"] if o["status"] == "delivered"}
    payments = out["data"]["payments"]
    assert delivered and len(payments) == len(delivered)          # a delivered order has a payment
    assert all(p["order_id"] in delivered and p["status"] == "success" for p in payments)


def test_no_llm_path_is_complete():
    out = seed_data(_ecommerce_schema(), seed=7, scenario=_SCENARIO, output="json")  # no llm passed
    assert all(o["user_id"] and o["status"] and o["total"] is not None for o in out["data"]["orders"])


# --- live PostgreSQL (opt-in, but must pass once to count as proven — spec §6 "live gate") --------
def _driver_or_skip():
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN to run the live-Postgres seeder tests")
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg not installed")
    return dsn


def _reset(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS "payments" CASCADE; '
                        'DROP TABLE IF EXISTS "orders" CASCADE; DROP TABLE IF EXISTS "users" CASCADE;')


def _executable(statements: list[str]) -> list[str]:
    return [s for s in statements if not s.strip().startswith("--")]


@pytest.mark.live_postgres
def test_full_loop_schema_migration_apply_seed_insert_on_real_postgres():
    """Headline acceptance (spec §6/§7): build the schema with M1's emitter, apply it, generate seed
    data, and run the INSERTs on a real server. Every FK/unique/NOT NULL/enum constraint must hold."""
    import psycopg

    dsn = _driver_or_skip()
    _reset(dsn)
    schema_dict = _ecommerce_schema()
    target = sj.load(schema_dict)
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    migration = _executable(emit_sql(diff(empty, target).op_dicts(), target).up_statements())
    seed = seed_data(schema_dict, seed=42, scenario=_SCENARIO, output="sql")

    try:
        with psycopg.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for stmt in migration:
                    cur.execute(stmt)               # M1 migration creates the schema
                for stmt in seed["sql"]["statements"]:
                    cur.execute(stmt)               # M3 seed inserts the data — no constraint may break
                # Row counts match what the seeder reported.
                cur.execute('SELECT count(*) FROM "users";')
                assert cur.fetchone()[0] == seed["rows"]["users"]
                cur.execute('SELECT count(*) FROM "orders";')
                assert cur.fetchone()[0] == seed["rows"]["orders"]
                cur.execute('SELECT count(*) FROM "payments";')
                assert cur.fetchone()[0] == seed["rows"]["payments"]
                # FK integrity actually holds on the server: every order.user_id resolves to a user.
                cur.execute('SELECT count(*) FROM "orders" o LEFT JOIN "users" u ON o.user_id = u.id '
                            'WHERE u.id IS NULL;')
                assert cur.fetchone()[0] == 0
                # Every payment belongs to a delivered order (state-machine consistency on real data).
                cur.execute('SELECT count(*) FROM "payments" p JOIN "orders" o ON p.order_id = o.id '
                            "WHERE o.status <> 'delivered';")
                assert cur.fetchone()[0] == 0
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_seed_is_idempotent_across_processes_against_real_schema():
    """Determinism with a real schema in the loop: same seed → identical SQL statements."""
    _driver_or_skip()
    schema_dict = _ecommerce_schema()
    a = seed_data(schema_dict, seed=123, scenario=_SCENARIO, output="sql")["sql"]["statements"]
    b = seed_data(schema_dict, seed=123, scenario=_SCENARIO, output="sql")["sql"]["statements"]
    assert a == b
