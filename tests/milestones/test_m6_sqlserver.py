"""Conformance kit — SQL Server live gate (spec §5, mandatory).

The third database closes the same loop against a **real SQL Server** that Postgres and MySQL do —
the proof that the M5 driver pattern actually generalises. These tests are opt-in (set
``VDB_TEST_SQLSERVER_DSN``, e.g. ``sqlserver://sa:Passw0rd!@127.0.0.1:1433/vdb_shadow``) but must pass
once on a real server to count as proven:

  * **round-trip** — design → emit T-SQL → apply on real SQL Server → import → equivalent schema;
  * **the FK lesson on SQL Server** — a uuid PK becomes ``UNIQUEIDENTIFIER`` and the FK column is the
    same ``UNIQUEIDENTIFIER``, and the constraint is actually created (no type-mismatch error);
  * **three-way drift** — the same six-category classification, via the MSSQL driver (introspect/apply
    through the driver pattern, not a separate code path), with the uuid PK never mis-reported as drift.

Postgres and MySQL are unaffected — their gates stay green unchanged (the zero-regression guarantee).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.drift import three_way_drift
from app.core.drivers import get_driver
from app.core.importer import build_schema_json
from app.core.sql_emitter import emit_sql

pytestmark = pytest.mark.conformance


def _reference_schema() -> dict:
    """users (uuid PK, email) ← orders (uuid PK, uuid FK, money). Every type round-trips cleanly on
    SQL Server (uuid↔UNIQUEIDENTIFIER, email↔varchar, money↔decimal); the FK column is declared uuid
    because that is what a uuid-keyed FK physically is, so the import infers it back identically."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "sqlserver", "defaultDriver": "sqlserver"},
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
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "uuid", "nullable": False},
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


def _fingerprint(schema_dict: dict) -> set[tuple]:
    """Structural fingerprint (table, column, semantic type) — order-independent equivalence."""
    s = sj.load(schema_dict, validate=False)
    return {(t.name, f.name, f.semantic_type) for t in s.logical.tables for f in t.fields}


def _dsn_or_skip() -> str:
    dsn = os.getenv("VDB_TEST_SQLSERVER_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_SQLSERVER_DSN to run the live-SQL Server tests")
    if importlib.util.find_spec("pymssql") is None and importlib.util.find_spec("pyodbc") is None:
        pytest.skip("no SQL Server driver installed (pymssql / pyodbc)")
    return dsn


def _up(designed: dict) -> list[str]:
    target = sj.load(designed)
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    return [s for s in emit_sql(diff(empty, target).op_dicts(), target, driver="sqlserver").up_statements()
            if not s.strip().startswith("--")]


@pytest.mark.live_sqlserver
def test_live_round_trip_emit_apply_import_is_equivalent():
    """design → emit T-SQL → apply on a real SQL Server → import → structurally equivalent."""
    dsn = _dsn_or_skip()
    drv = get_driver("sqlserver")
    drv.reset(dsn)
    try:
        drv.apply_sql(dsn, _up(_reference_schema()))
        imported = build_schema_json(drv.introspect(dsn), driver="sqlserver")["schema_json"]
        assert _fingerprint(_reference_schema()) == _fingerprint(imported)
    finally:
        drv.reset(dsn)


@pytest.mark.live_sqlserver
def test_live_uuid_fk_is_uniqueidentifier_and_constraint_is_created():
    """The M1 bug, now on SQL Server: a uuid FK must be UNIQUEIDENTIFIER and the constraint must build."""
    dsn = _dsn_or_skip()
    drv = get_driver("sqlserver")
    drv.reset(dsn)
    try:
        drv.apply_sql(dsn, _up(_reference_schema()))  # raises if SQL Server rejects the FK type
        introspected = drv.introspect(dsn)
        cols = {(t.name, c.name): c for t in introspected.tables for c in t.columns}
        assert cols[("users", "id")].data_type.lower() == "uniqueidentifier"
        assert cols[("orders", "user_id")].data_type.lower() == "uniqueidentifier"  # FK == PK type
        assert any(fk.table == "orders" and fk.ref_table == "users" for fk in introspected.foreign_keys)
    finally:
        drv.reset(dsn)


def _drift_design() -> dict:
    """Leg A (designed): users(id uuid, email, note) + a not-yet-migrated drafts table. The uuid PK is
    UNIQUEIDENTIFIER on SQL Server — the FK lesson the drift comparison must respect (no type drift)."""
    return sj.load({
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "sqlserver", "defaultDriver": "sqlserver"},
        "logical": {"tables": [
            {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                 "isPrimaryKey": True, "nullable": False},
                {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                {"id": "fld_unote0001", "name": "note", "semanticType": "string", "nullable": True},
            ]},
            {"id": "tbl_drafts001", "name": "drafts", "kind": "normal", "fields": [
                {"id": "fld_did000001", "name": "id", "semanticType": "uuid",
                 "isPrimaryKey": True, "nullable": False},
            ]},
        ]},
    }, validate=False)


@pytest.mark.live_sqlserver
def test_live_sqlserver_three_way_drift_reports_categories_and_respects_fk_type():
    """drift on a **real SQL Server** (multi-driver §4): the same six-category classification Postgres
    gives, via the driver pattern (introspect/apply through the MSSQL driver), and the FK lesson — a uuid
    PK that is UNIQUEIDENTIFIER on SQL Server is *not* mis-reported as type drift.

    Legs are built by applying DDL and introspecting (resetting the shadow DB between), so one dedicated
    SQL Server database is enough; it is a throwaway shadow, never a real one."""
    dsn = _dsn_or_skip()
    drv = get_driver("sqlserver")
    try:
        # Leg B (migrations): users(id, email, note).
        drv.reset(dsn)
        drv.apply_sql(dsn, ["CREATE TABLE [users] ([id] UNIQUEIDENTIFIER NOT NULL, "
                            "[email] NVARCHAR(255) NOT NULL, [note] NVARCHAR(255) NULL, "
                            "CONSTRAINT [PK_users] PRIMARY KEY ([id]));"])
        migrations = sj.load(build_schema_json(drv.introspect(dsn), driver="sqlserver")["schema_json"],
                             validate=False)
        # Leg C (live): note never applied; a manual `hotfix` column was added straight on prod instead.
        drv.reset(dsn)
        drv.apply_sql(dsn, ["CREATE TABLE [users] ([id] UNIQUEIDENTIFIER NOT NULL, "
                            "[email] NVARCHAR(255) NOT NULL, [hotfix] NVARCHAR(255) NULL, "
                            "CONSTRAINT [PK_users] PRIMARY KEY ([id]));"])
        live = sj.load(build_schema_json(drv.introspect(dsn), driver="sqlserver")["schema_json"],
                       validate=False)

        report = three_way_drift(_drift_design(), migrations, live, driver="sqlserver")
        by_entity = {d.entity: d.category for d in report.drift}
        assert by_entity.get("users.note") == "migration_not_applied"
        assert by_entity.get("users.hotfix") == "manual_prod_change"
        assert by_entity.get("drafts") == "design_ahead_of_code"
        assert report.exit_code == 1
        # The FK lesson on SQL Server: the uuid PK (UNIQUEIDENTIFIER) is synced across all legs, never drift.
        assert "users.id" not in by_entity
    finally:
        drv.reset(dsn)
