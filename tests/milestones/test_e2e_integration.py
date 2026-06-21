"""Conformance kit — End-to-End Integration (the "everything connects" milestone, spec §4/§5).

This milestone builds **no new feature**; it proves the contracts between the already-proven subsystems
(Core, M1 migration, M2 import/drift, M3 seed, M4 API) actually line up when run back-to-back on one
real database. So the headline is, once more, a **live gate**: a real PRD → the whole 9-step chain →
``result == "green"`` with the migration applied, the seed inserted, and the generated API answering
real HTTP — no manual fix-ups between steps. Plus: the brownfield path (import→drift→seed→API), the
approval gate (without approval nothing downstream runs), determinism, and the contract-leak guard (the
*same* approved schema flows through every step untouched).
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from app.core import e2e as core_e2e
from app.core import schema_json as sj
from app.core.design_session import InvalidTransitionError, SessionStore
from app.core.diff import diff
from app.core.sql_emitter import emit_sql

pytestmark = pytest.mark.conformance

_PRD = "An online store with users, orders and payments."


def _brownfield_schema() -> dict:
    """A small real schema (uuid PK + a uuid FK) used to populate a DB before the brownfield path."""
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
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
    }


# --- offline acceptance (no database needed) ----------------------------------------------------


def test_approval_gate_blocks_everything_downstream():
    """The one immovable boundary (spec §4): with ``autoApprove=False`` the chain pauses at approval and
    runs *nothing* downstream — no migration, seed or API. The DSN is never even touched."""
    store = SessionStore()
    result = anyio_run(core_e2e.run_e2e(store, mode="greenfield", prd=_PRD,
                                        dsn="postgresql://unused", auto_approve=False, llm=None))
    assert result["result"] == "awaiting_approval"
    step_names = [s["step"] for s in result["steps"]]
    assert step_names == ["suggest", "validate", "approve"]
    assert result["steps"][-1] == {"step": "approve", "ok": False, "reason": "awaiting_human_approval"}
    assert "migration" not in step_names and "seed" not in step_names and "api" not in step_names
    # And the hard guard still holds at the session level: migration is refused until approved.
    with pytest.raises(InvalidTransitionError):
        store.migration(result["sessionId"])


def test_deterministic_prefix_with_no_llm():
    """Same PRD, no LLM → byte-identical steps (only the LLM text in *suggest* could vary; here none)."""
    a = anyio_run(core_e2e.run_e2e(SessionStore(), prd=_PRD, dsn="postgresql://unused",
                                   auto_approve=False, llm=None))
    b = anyio_run(core_e2e.run_e2e(SessionStore(), prd=_PRD, dsn="postgresql://unused",
                                   auto_approve=False, llm=None))
    assert _strip_volatile(a) == _strip_volatile(b)


def test_no_manual_translation_one_schema_flows_through():
    """Contract-leak guard (spec §5): the schema is never re-shaped between steps. The suggestion the AI
    step produced, the schema the approved session locked, and the handoff schema_json are all identical
    — so migration/seed/API downstream all consume the very same artifact."""
    store = SessionStore()
    # Drive the real session lifecycle offline (approve/handoff are pure; only apply touches a DB).
    import anyio

    suggestion = anyio.run(_suggest_only, store)
    session = store.get(suggestion["sessionId"])
    locked = session.schema_doc
    handoff = store.handoff(session.id)
    assert locked == handoff["schema_json"]                 # approval locked exactly what was applied
    assert handoff["schema_json"] == suggestion["schema"]   # which is exactly the AI suggestion


# --- live PostgreSQL (opt-in, but must pass once to count as proven — spec §4 "live gate") -------
def _driver_or_skip() -> str:
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN to run the live end-to-end integration test")
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg not installed")
    return dsn


def _reset(dsn: str) -> None:
    """A clean slate for the whole chain: drop and recreate the public schema (dedicated test DB)."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _apply_schema(dsn: str, schema_dict: dict) -> None:
    import psycopg

    target = sj.load(schema_dict)
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    up = [s for s in emit_sql(diff(empty, target).op_dicts(), target).up_statements()
          if not s.strip().startswith("--")]
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in up:
                cur.execute(stmt)


@pytest.mark.live_postgres
def test_full_greenfield_chain_is_green_on_real_postgres():
    """Headline acceptance (spec §4): one PRD → the full 9-step chain → ``green``, on a real database,
    with the migration applied, the seed inserted and the generated API answering real HTTP."""
    import psycopg

    dsn = _driver_or_skip()
    _reset(dsn)
    try:
        result = anyio_run(core_e2e.run_e2e(
            SessionStore(), mode="greenfield", prd=_PRD, dsn=dsn, auto_approve=True,
            scenario={"name": "ecommerce_medium"}, llm=None))
        assert result["result"] == "green", result
        steps = {s["step"]: s for s in result["steps"]}
        assert [s["step"] for s in result["steps"]] == ["suggest", "validate", "approve",
                                                        "migration", "seed", "api"]
        assert all(s["ok"] for s in result["steps"])
        assert steps["migration"]["applied"] is True
        assert result["schemaVersion"] == "v1"

        # The API served exactly the rows the seed step reported, and a real POST created a row.
        rows = steps["seed"]["rows"]
        sample = steps["api"]["sample"]
        assert sample["GET /v1/orders"] == rows["orders"]
        assert sample["GET /v1/users"] == rows["users"]
        assert sample["POST /v1/orders"] == 201

        # The POST actually landed in the database (one more order than the seed inserted).
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM "orders";')
            assert cur.fetchone()[0] == rows["orders"] + 1
            # Referential integrity holds on the server: every order points at a real user.
            cur.execute('SELECT count(*) FROM "orders" o LEFT JOIN "users" u ON o.user_id = u.id '
                        'WHERE u.id IS NULL;')
            assert cur.fetchone()[0] == 0
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_brownfield_chain_is_green_on_real_postgres():
    """The brownfield path (spec §2): an existing DB → import → drift (clean) → seed → API, all green."""
    dsn = _driver_or_skip()
    _reset(dsn)
    try:
        _apply_schema(dsn, _brownfield_schema())   # an existing database to discover
        result = anyio_run(core_e2e.run_e2e(SessionStore(), mode="brownfield", dsn=dsn,
                                            auto_approve=True, llm=None))
        assert result["result"] == "green", result
        steps = {s["step"]: s for s in result["steps"]}
        assert [s["step"] for s in result["steps"]] == ["import", "drift", "validate", "approve",
                                                        "migration", "seed", "api"]
        assert steps["drift"]["exitCode"] == 0          # designed == live → no drift
        assert steps["import"]["tables"] == 2
        assert steps["seed"]["rows"]["users"] >= 1
        assert steps["api"]["sample"]["GET /v1/users"] == steps["seed"]["rows"]["users"]
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_full_chain_is_deterministic_across_two_runs():
    """Determinism (spec §4): same PRD + autoApprove + seed → the same reproducible result."""
    dsn = _driver_or_skip()
    _reset(dsn)
    try:
        a = anyio_run(core_e2e.run_e2e(SessionStore(), prd=_PRD, dsn=dsn, auto_approve=True,
                                       scenario={"name": "ecommerce_medium"}, llm=None))
        _reset(dsn)
        b = anyio_run(core_e2e.run_e2e(SessionStore(), prd=_PRD, dsn=dsn, auto_approve=True,
                                       scenario={"name": "ecommerce_medium"}, llm=None))
        assert a["result"] == b["result"] == "green"
        assert _strip_volatile(a) == _strip_volatile(b)
    finally:
        _reset(dsn)


# --- helpers ------------------------------------------------------------------------------------
def _strip_volatile(result: dict) -> str:
    """Compare two runs ignoring the only non-deterministic field (the random session id)."""
    clone = json.loads(json.dumps(result))
    clone.pop("sessionId", None)
    return json.dumps(clone, sort_keys=True)


async def _suggest_only(store: SessionStore) -> dict:
    """Run only the offline prefix (create → suggest → apply → validate → submit → approve) so the
    contract-leak test can compare the suggestion, the locked schema and the handoff without a DB."""
    from app.core import suggest as core_suggest

    session = store.create(mode="greenfield", prd=_PRD)
    suggested = await core_suggest.suggest_schema(session.prd or "", llm=None)
    store.apply_schema(session.id, suggested["suggestion"])
    store.validate(session.id)
    store.submit(session.id)
    store.approve(session.id, approved_by="e2e-auto")
    # ``apply_schema`` normalises via migrate(); compare against the same normalisation of the suggestion.
    return {"sessionId": session.id, "schema": sj.migrate(suggested["suggestion"])}


def anyio_run(coro):
    import anyio

    return anyio.run(lambda: coro)
