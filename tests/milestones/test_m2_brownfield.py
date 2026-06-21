"""Conformance kit — Milestone 2: Brownfield Import + Three-Way Drift (spec §4/§5).

The acceptance gate (marked ``conformance``), distinct from the unit tests. It proves the brownfield
contract:

  * a live database introspects to a valid, deterministic ``schema_json``;
  * the **round-trip** ``emit (M1) → apply → import (M2) → compare`` is structurally equivalent — this
    is the loop that locks the emitter and importer together;
  * a known three-way drift scenario is categorised correctly across all of the spec's cases;
  * reconcile matches without shared ids and flags the ambiguous case instead of guessing.

As in Milestone 1, the live-Postgres tests are opt-in (set ``VDB_TEST_POSTGRES_DSN``) but **must pass
once on a real server** to count as proven — a snapshot alone is not proof (the lesson M1 taught).
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from app.core import drift as core_drift
from app.core import schema_json as sj
from app.core.diff import diff
from app.core.drift import reconcile, three_way_drift
from app.core.importer import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedSchema,
    IntrospectedTable,
    apply_sql,
    build_schema_json,
    import_sql_via_shadow,
    introspect_postgres,
)
from app.core.sql_emitter import emit_sql
from app.core.type_system import DEFAULT_REGISTRY

pytestmark = pytest.mark.conformance


def _reference_schema() -> dict:
    """The same small greenfield schema M1 uses — so the round-trip closes the M1↔M2 loop."""
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


def _reference_introspected() -> IntrospectedSchema:
    """What introspecting the applied ``_reference_schema`` should yield (drives the import snapshot)."""
    return IntrospectedSchema(
        tables=[
            IntrospectedTable(name="users", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="uuid", nullable=False),
                IntrospectedColumn(name="email", data_type="character varying", char_max_length=255, nullable=False),
            ]),
            IntrospectedTable(name="orders", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="uuid", nullable=False),
                IntrospectedColumn(name="user_id", data_type="uuid", nullable=False),
                IntrospectedColumn(name="total", data_type="numeric", numeric_precision=12,
                                   numeric_scale=2, nullable=False),
            ]),
        ],
        foreign_keys=[IntrospectedForeignKey(
            name="orders_user_id_fkey", table="orders", columns=["user_id"],
            ref_table="users", ref_columns=["id"], on_delete="cascade")],
    )


# --------------------------------------------------------------------------------------------------
# Structural fingerprint — the equivalence used by the round-trip (semantic-type labels may differ;
# the *structure* — physical types, PKs, relations — must not).
# --------------------------------------------------------------------------------------------------
def _fingerprint(schema_json: dict) -> dict:
    s = sj.load(schema_json, validate=False)
    physical = core_drift._physical_map(s, DEFAULT_REGISTRY)
    tables = {
        t.name: {
            "columns": {f.name: physical[(t.name, f.name)] for f in t.fields},
            "pk": sorted(f.name for f in t.primary_keys()),
        }
        for t in s.logical.tables
    }
    relations = sorted(
        [
            s.table_by_id(r.from_table_id).name if s.table_by_id(r.from_table_id) else None,
            s.table_by_id(r.to_table_id).name if (r.to_table_id and s.table_by_id(r.to_table_id)) else None,
            (s.field_by_id(r.foreign_key_field_id)[1].name if r.foreign_key_field_id
             and s.field_by_id(r.foreign_key_field_id) else None),
            r.type,
        ]
        for r in s.logical.relations
    )
    return {"tables": tables, "relations": relations}


# --- import snapshot + determinism (no DB) --------------------------------------------------------


def test_import_snapshot_is_valid_and_stable():
    out = build_schema_json(_reference_introspected(), name="shop")
    assert out["validation"]["structuralErrors"] == []
    assert out["validation"]["summary"]["error"] == 0
    # Deterministic: building twice is byte-identical (spec §4 determinism).
    again = build_schema_json(_reference_introspected(), name="shop")
    assert json.dumps(out["schema_json"], sort_keys=True) == json.dumps(again["schema_json"], sort_keys=True)


def test_round_trip_fingerprint_emit_then_import_is_equivalent():
    """Offline half of the round-trip: emitting the reference and importing the *expected* introspection
    of it must be structurally equivalent. The live half (apply on a real server) is below."""
    designed = _reference_schema()
    imported = build_schema_json(_reference_introspected())["schema_json"]
    assert _fingerprint(designed) == _fingerprint(imported)


# --- three-way drift: every category in one report (spec §4 "Three-Way") --------------------------


def _t(tid: str, name: str, cols: list[tuple[str, str]]) -> dict:
    fields = []
    for i, (cname, stype) in enumerate(cols):
        f = {"id": f"fld_{tid[-3:]}{i:03d}", "name": cname, "semanticType": stype, "nullable": cname != "id"}
        if cname == "id":
            f["isPrimaryKey"] = True
        fields.append(f)
    return {"id": tid, "name": name, "kind": "normal", "fields": fields}


def _schema(tables: list[dict]) -> sj.SchemaJson:
    return sj.load({"formatVersion": "1.0.0", "logical": {"tables": tables}}, validate=False)


def test_three_way_categorises_all_scenarios():
    # users.{id,email} synced; users.phone designed+migrated but not live (not applied);
    # users.hotfix live-only (manual prod); users.age inconsistent (incomplete);
    # drafts designed-only (design ahead); audit migrated+live but not designed (code ahead).
    a = _schema([
        _t("tbl_a01", "users", [("id", "uuid"), ("email", "email"), ("phone", "string"), ("age", "integer")]),
        _t("tbl_a02", "drafts", [("id", "uuid")]),
    ])
    b = _schema([
        _t("tbl_b01", "users", [("id", "uuid"), ("email", "email"), ("phone", "string"), ("age", "big_integer")]),
        _t("tbl_b02", "audit", [("id", "uuid")]),
    ])
    c = _schema([
        _t("tbl_c01", "users", [("id", "uuid"), ("email", "email"), ("hotfix", "string"), ("age", "integer")]),
        _t("tbl_c02", "audit", [("id", "uuid")]),
    ])
    report = three_way_drift(a, b, c)
    by_entity = {d.entity: d.category for d in report.drift}
    assert by_entity["users.phone"] == "migration_not_applied"
    assert by_entity["users.hotfix"] == "manual_prod_change"
    assert by_entity["drafts"] == "design_ahead_of_code"
    assert by_entity["audit"] == "code_ahead_of_design"
    assert by_entity["users.age"] == "migration_incomplete"
    assert "users.id" not in by_entity and "users.email" not in by_entity  # synced → no entry
    assert report.exit_code == 1  # has error-severity drift


def test_reconcile_no_shared_id_and_ambiguous():
    a = _schema([_t("tbl_designed", "users", [("id", "uuid")]),
                 _t("tbl_cust", "customers", [("id", "uuid"), ("email", "email")])])
    c = _schema([_t("tbl_live1", "users", [("id", "uuid")]),
                 _t("tbl_live2", "clients", [("id", "uuid"), ("email", "email")])])
    rec = reconcile(a, None, c)
    assert rec.matched == 1  # users matched by name
    assert any(m.entity == "customers" for m in rec.ambiguous)  # customers↔clients flagged, not guessed


# --------------------------------------------------------------------------------------------------
# Live PostgreSQL (opt-in, but must pass once to count as proven — spec §4 "Gate live").
# --------------------------------------------------------------------------------------------------
def _driver_or_skip():
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN to run the live-Postgres brownfield tests")
    if importlib.util.find_spec("psycopg") is None and importlib.util.find_spec("psycopg2") is None:
        pytest.skip("no psycopg/psycopg2 driver installed")
    return dsn


def _reset(dsn: str, schemas: tuple[str, ...] = ("public",)) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for s in schemas:
                if s == "public":
                    cur.execute('DROP TABLE IF EXISTS "orders" CASCADE; DROP TABLE IF EXISTS "users" CASCADE;')
                else:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE; CREATE SCHEMA "{s}";')


@pytest.mark.live_postgres
def test_live_import_is_valid_and_deterministic():
    dsn = _driver_or_skip()
    _reset(dsn)
    target = sj.load(_reference_schema())
    apply_sql(dsn, [s for s in emit_sql(diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}),
                                            target).op_dicts(), target).up_statements()
                    if not s.strip().startswith("--")])
    try:
        first = build_schema_json(introspect_postgres(dsn), name="shop")
        assert first["validation"]["summary"]["error"] == 0
        second = build_schema_json(introspect_postgres(dsn), name="shop")
        assert json.dumps(first["schema_json"], sort_keys=True) == json.dumps(second["schema_json"], sort_keys=True)
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_live_round_trip_emit_apply_import_is_equivalent():
    """The headline acceptance: design → emit (M1) → apply on a real server → import (M2) → compare."""
    dsn = _driver_or_skip()
    _reset(dsn)
    designed = _reference_schema()
    target = sj.load(designed)
    up = [s for s in emit_sql(diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}),
                                   target).op_dicts(), target).up_statements()
          if not s.strip().startswith("--")]
    try:
        apply_sql(dsn, up)
        imported = build_schema_json(introspect_postgres(dsn))["schema_json"]
        assert _fingerprint(designed) == _fingerprint(imported)
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_live_file_import_via_shadow_db_preserves_uuid_fk():
    """File import (§2): a raw SQL/DDL dump → shadow DB → introspect → schema_json. The whole point
    of the project: a uuid foreign key survives the round-trip as ``uuid``, never an integer."""
    dsn = _driver_or_skip()
    _reset(dsn)
    dump = """
        CREATE TABLE users (id uuid PRIMARY KEY, email varchar(255) NOT NULL);
        CREATE TABLE orders (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            total numeric(12,2) NOT NULL
        );
    """
    try:
        result = import_sql_via_shadow(dump, dsn, name="shop")
        schema = result["schema_json"]
        assert {t["name"] for t in schema["logical"]["tables"]} == {"users", "orders"}
        # The FK column's physical type is read straight from the database → uuid (not integer).
        s = sj.load(schema, validate=False)
        physical = core_drift._physical_map(s, DEFAULT_REGISTRY)
        assert physical[("orders", "user_id")] == "uuid"
        # A real relation was rebuilt from the FK constraint.
        assert any(r.type in {"one_to_many", "one_to_one"} for r in s.logical.relations)
        # Deterministic: importing the same dump again is byte-identical.
        again = import_sql_via_shadow(dump, dsn, name="shop")
        assert json.dumps(result["schema_json"], sort_keys=True) == json.dumps(again["schema_json"], sort_keys=True)
    finally:
        _reset(dsn)


@pytest.mark.live_postgres
def test_live_three_way_drift_over_real_databases():
    """Leg B via a shadow schema (apply raw SQL then introspect), Leg C the live schema, Leg A authored."""
    dsn = _driver_or_skip()
    _reset(dsn, ("public", "vdb_shadow"))
    try:
        # Leg B (migrations applied to a shadow schema): users(id, email, phone).
        apply_sql(dsn, [
            'CREATE TABLE "vdb_shadow"."users" (id uuid PRIMARY KEY, email varchar(255), phone varchar(20));',
        ])
        migrations = sj.load(build_schema_json(introspect_postgres(dsn, schema="vdb_shadow"))["schema_json"],
                             validate=False)
        # Leg C (live): phone never applied; hotfix added by hand.
        apply_sql(dsn, [
            'CREATE TABLE "users" (id uuid PRIMARY KEY, email varchar(255), hotfix varchar(20));',
        ])
        live = sj.load(build_schema_json(introspect_postgres(dsn))["schema_json"], validate=False)
        # Leg A (designed): wants phone + a not-yet-migrated drafts table.
        designed = _schema([
            _t("tbl_a01", "users", [("id", "uuid"), ("email", "email"), ("phone", "string")]),
            _t("tbl_a02", "drafts", [("id", "uuid")]),
        ])
        report = three_way_drift(designed, migrations, live)
        by_entity = {d.entity: d.category for d in report.drift}
        assert by_entity.get("users.phone") == "migration_not_applied"
        assert by_entity.get("users.hotfix") == "manual_prod_change"
        assert by_entity.get("drafts") == "design_ahead_of_code"
        assert report.exit_code == 1
    finally:
        _reset(dsn, ("public", "vdb_shadow"))
