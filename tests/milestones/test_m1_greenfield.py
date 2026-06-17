"""Conformance kit — Milestone 1: Greenfield Walking Skeleton (spec §10/§11).

This is the *acceptance gate* (marked ``conformance``), distinct from the unit tests. It proves the
contract end-to-end over the HTTP surface: PRD → suggest → apply → validate → submit → approve →
migration (executable SQL) → handoff. The negative gate tests matter more than the positive one:

  1. /migration and /handoff on a non-approved session → 409.
  2. approve cannot be reached past a validation error.
  3. a critical operation (drop_table) is blocked unless explicitly acknowledged.
  4. the whole pipeline runs with no LLM involvement at all.

Plus: determinism (byte-identical re-run) and an SQL snapshot. A live-Postgres execution test runs
only when ``VDB_TEST_POSTGRES_DSN`` is set (and a driver is installed), otherwise it is skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.sql_emitter import emit_sql
from app.module import app

pytestmark = pytest.mark.conformance

client = TestClient(app)

PRD = "یک سیستم فروشگاهی با کاربران، سفارش‌ها و پرداخت‌ها"


def _reference_schema() -> dict:
    """A small, self-contained greenfield schema used by the gate + snapshot fixtures."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
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
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False},
                ]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
    }


def _new_session(prd: str = PRD) -> str:
    return client.post("/design/sessions", json={"mode": "greenfield", "prd": prd}).json()["sessionId"]


def _drive_to_approved(schema: dict, approved_by: str = "user_123", **approve_kw) -> str:
    sid = client.post("/design/sessions", json={"mode": "greenfield", "schema_json": schema}).json()["sessionId"]
    client.post(f"/design/sessions/{sid}/validate")
    client.post(f"/design/sessions/{sid}/submit")
    client.post(f"/design/sessions/{sid}/approve", json={"approvedBy": approved_by, **approve_kw})
    return sid


# --- positive path --------------------------------------------------------------------------------


def test_positive_prd_to_handoff():
    sid = _new_session()
    suggest = client.post(f"/design/sessions/{sid}/suggest").json()
    assert suggest["suggestion"]["logical"]["tables"]  # AI proposed something
    assert suggest["diffFromCurrent"]  # shown to the human as empty → suggestion

    apply = client.post(f"/design/sessions/{sid}/apply-suggestion",
                        json={"schema_json": suggest["suggestion"]}).json()
    assert apply["state"] == "draft"

    validate = client.post(f"/design/sessions/{sid}/validate").json()
    assert validate["state"] == "validated"
    assert validate["report"]["summary"]["error"] == 0

    assert client.post(f"/design/sessions/{sid}/submit").json()["state"] == "pending_approval"

    approve = client.post(f"/design/sessions/{sid}/approve", json={"approvedBy": "user_123"}).json()
    assert approve["state"] == "approved" and approve["schemaVersion"] == "v1"
    assert approve["checksum"].startswith("sha256:")

    migration = client.get(f"/design/sessions/{sid}/migration").json()
    assert migration["sql"]["driver"] == "postgres"
    assert any("CREATE TABLE" in s for s in migration["sql"]["steps"])

    handoff = client.get(f"/design/sessions/{sid}/handoff").json()
    assert handoff["status"] == "approved"
    assert handoff["validation"]["summary"]["error"] == 0
    assert handoff["migration"]["sql"]["steps"]
    assert handoff["checksum"] == approve["checksum"]


# --- negative gate tests (the important ones) -----------------------------------------------------


def test_negative_migration_and_handoff_require_approved():
    sid = _new_session()
    client.post(f"/design/sessions/{sid}/apply-suggestion", json={"schema_json": _reference_schema()})
    assert client.get(f"/design/sessions/{sid}/migration").status_code == 409
    assert client.get(f"/design/sessions/{sid}/handoff").status_code == 409
    assert client.get(f"/design/sessions/{sid}/migration").json()["error"] == "not_approved"


def test_negative_approve_blocked_by_validation_error():
    broken = _reference_schema()
    broken["logical"]["relations"][0]["toTableId"] = "tbl_missing01"  # REF002 error
    sid = client.post("/design/sessions", json={"schema_json": broken}).json()["sessionId"]
    validate = client.post(f"/design/sessions/{sid}/validate").json()
    assert validate["state"] == "draft"  # never becomes validated
    # submit is rejected because the session is not validated → the gate cannot be reached.
    assert client.post(f"/design/sessions/{sid}/submit").status_code == 409


def test_negative_approve_from_draft_is_409():
    sid = _new_session()
    client.post(f"/design/sessions/{sid}/apply-suggestion", json={"schema_json": _reference_schema()})
    resp = client.post(f"/design/sessions/{sid}/approve", json={"approvedBy": "user_123"})
    assert resp.status_code == 409


def test_negative_critical_drop_table_needs_acknowledgement():
    approved = _drive_to_approved(_reference_schema())
    revision = client.post(f"/design/sessions/{approved}/revise").json()["sessionId"]
    dropped = _reference_schema()
    dropped["logical"]["tables"] = [dropped["logical"]["tables"][0]]  # drop orders
    dropped["logical"]["relations"] = []
    client.post(f"/design/sessions/{revision}/apply-suggestion", json={"schema_json": dropped})
    client.post(f"/design/sessions/{revision}/validate")
    client.post(f"/design/sessions/{revision}/submit")

    blocked = client.post(f"/design/sessions/{revision}/approve", json={"approvedBy": "user_123"})
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["error"] == "gate_blocked" and body["reason"] == "critical_migration_risk"
    assert any(b["op"] == "drop_table" for b in body["blocking"])

    ack = client.post(f"/design/sessions/{revision}/approve",
                      json={"approvedBy": "user_123", "acknowledgeCritical": True})
    assert ack.status_code == 200 and ack.json()["state"] == "approved"


def test_negative_pipeline_runs_with_no_llm():
    """A user-supplied schema goes through validate/submit/approve/migration/handoff with no /suggest."""
    sid = client.post("/design/sessions", json={"schema_json": _reference_schema()}).json()["sessionId"]
    assert client.post(f"/design/sessions/{sid}/validate").json()["state"] == "validated"
    client.post(f"/design/sessions/{sid}/submit")
    assert client.post(f"/design/sessions/{sid}/approve",
                       json={"approvedBy": "user_123"}).json()["state"] == "approved"
    handoff = client.get(f"/design/sessions/{sid}/handoff").json()
    assert handoff["status"] == "approved" and handoff["migration"]["sql"]["steps"]


# --- determinism ----------------------------------------------------------------------------------


def test_migration_is_byte_identical_across_runs():
    sid = _drive_to_approved(_reference_schema())
    first = client.get(f"/design/sessions/{sid}/migration").json()
    second = client.get(f"/design/sessions/{sid}/migration").json()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- SQL snapshot ---------------------------------------------------------------------------------


def test_sql_emitter_snapshot():
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    target = sj.load(_reference_schema())
    up = emit_sql(diff(empty, target).op_dicts(), target).up_statements()
    assert up == [
        'CREATE TABLE "orders" (\n'
        '  "id" uuid NOT NULL,\n'
        '  "user_id" uuid NOT NULL,\n'
        '  "total" numeric(12,2) NOT NULL,\n'
        '  PRIMARY KEY ("id")\n'
        ');',
        'CREATE TABLE "users" (\n'
        '  "id" uuid NOT NULL,\n'
        '  "email" varchar(255) NOT NULL,\n'
        '  PRIMARY KEY ("id")\n'
        ');',
        'ALTER TABLE "orders" ADD CONSTRAINT "orders_user_id_fkey" '
        'FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE;',
    ]


# --- live PostgreSQL execution (opt-in) -----------------------------------------------------------


@pytest.mark.live_postgres
def test_emitted_sql_runs_on_real_postgres():
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN to run the live-Postgres execution test")
    driver_mod = next((m for m in ("psycopg", "psycopg2") if importlib.util.find_spec(m)), None)
    if driver_mod is None:
        pytest.skip("no psycopg/psycopg2 driver installed")
    psycopg = __import__(driver_mod)

    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    target = sj.load(_reference_schema())
    script = emit_sql(diff(empty, target).op_dicts(), target)

    def _exec_all(cur, statements):
        for stmt in statements:
            if stmt.strip().startswith("--"):  # narrative/rollback comments are not executable
                continue
            cur.execute(stmt)

    conn = psycopg.connect(dsn)
    try:
        conn.autocommit = True  # CREATE INDEX CONCURRENTLY cannot run inside a transaction
        with conn.cursor() as cur:
            # Start from a clean slate so a crashed prior run can't poison this one.
            cur.execute('DROP TABLE IF EXISTS "orders" CASCADE; DROP TABLE IF EXISTS "users" CASCADE;')

            # 1. The up migration runs to completion and the schema exists.
            _exec_all(cur, script.up_statements())
            cur.execute("SELECT to_regclass('public.users'), to_regclass('public.orders');")
            assert all(cur.fetchone()), "up migration did not create both tables"

            # 2. The down migration runs to completion and actually removes the schema (an
            #    incomplete rollback only shows up here, on a real server — never in a snapshot).
            _exec_all(cur, script.down_statements())
            cur.execute("SELECT to_regclass('public.users'), to_regclass('public.orders');")
            assert not any(cur.fetchone()), "down migration left objects behind"
    finally:
        with conn.cursor() as cur:  # leave the database as we found it, even on failure
            cur.execute('DROP TABLE IF EXISTS "orders" CASCADE; DROP TABLE IF EXISTS "users" CASCADE;')
        conn.close()
