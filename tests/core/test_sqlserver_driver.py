"""Conformance kit — SQL Server driver + the driver pattern (multi-driver milestone, third database).

This is the *architecture test* the milestone exists for: a third database proves the M5 pattern by
adding only a driver module + a registry line, with the Core untouched. Two things are proven here
without a server (the live gate in :mod:`tests.milestones.test_m6_sqlserver` proves them against real
SQL Server):

* the **emit** side — T-SQL is correct dialect (``[brackets]``, ``IDENTITY(1,1)`` folded into the
  type, ``BIT``, ``DATETIME2``, no ``CONCURRENTLY``) and the **FK lesson** holds: a uuid PK is
  ``UNIQUEIDENTIFIER`` and its foreign-key column is the *same* ``UNIQUEIDENTIFIER`` (spec §1);
* the **import** side — the reverse type map turns native MSSQL columns back into semantic types
  (``UNIQUEIDENTIFIER`` → uuid, ``BIT`` → boolean, ``IDENTITY`` → integer key) so the emit↔import
  round-trip closes (spec §1/§3).
"""

from __future__ import annotations

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.drivers import get_dialect, get_driver
from app.core.drivers.base import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedSchema,
    IntrospectedTable,
)
from app.core.importer import build_schema_json
from app.core.sql_emitter import emit_sql
from app.core.type_system import DEFAULT_REGISTRY, resolve_fk_physical


def _schema() -> dict:
    """users (uuid PK, email, is_active boolean, identity counter) ← orders.user_id (uuid FK)."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "sqlserver", "defaultDriver": "sqlserver"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                    {"id": "fld_uactive01", "name": "is_active", "semanticType": "boolean", "nullable": False},
                    {"id": "fld_uhits0001", "name": "hits", "semanticType": "integer",
                     "nullable": False, "autoIncrement": True},
                    {"id": "fld_ucreated1", "name": "created_at", "semanticType": "datetime", "nullable": False},
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
        "physical": {"indexes": [
            {"id": "idx_uemail001", "tableId": "tbl_users0001", "columns": ["fld_uemail001"], "unique": True},
        ]},
    }


def _sqlserver_up() -> str:
    target = sj.load(_schema())
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    ops = diff(empty, target).op_dicts()
    return "\n".join(emit_sql(ops, target, driver="sqlserver").up_statements())


# --- the driver pattern ---------------------------------------------------------------------------


def test_registry_resolves_sqlserver_and_alias():
    assert get_dialect("sqlserver").quote_char == "["
    assert get_dialect("sqlserver").q("users") == "[users]"
    assert get_driver("mssql").name == "sqlserver"        # mssql is the common short name
    assert get_driver("sqlserver").default_schema == "dbo"


# --- emit: T-SQL dialect --------------------------------------------------------------------------


def test_sqlserver_dialect_basics():
    up = _sqlserver_up()
    assert "CREATE TABLE [users]" in up
    assert "[email] nvarchar(255) NOT NULL" in up           # strings → NVARCHAR (Unicode-safe by default)
    assert "[is_active] bit NOT NULL" in up                 # boolean → BIT
    assert "[total] decimal(12,2) NOT NULL" in up           # money → DECIMAL
    assert "[created_at] datetime2 NOT NULL" in up          # datetime → DATETIME2 (not T-SQL's rowversion)
    assert "[hits] integer IDENTITY(1,1) NOT NULL" in up    # identity folded into the type, before NOT NULL
    assert "CREATE UNIQUE INDEX [users_email_uniq] ON [users]" in up
    assert "CONCURRENTLY" not in up and "`" not in up and '"' not in up


def test_sqlserver_fk_column_matches_uuid_pk_as_uniqueidentifier():
    """The FK lesson, on SQL Server: a uuid PK is UNIQUEIDENTIFIER and the FK column is the SAME type."""
    up = _sqlserver_up()
    assert "[id] uniqueidentifier NOT NULL" in up           # uuid PK → UNIQUEIDENTIFIER
    assert "[user_id] uniqueidentifier NOT NULL" in up      # uuid FK inherits exactly UNIQUEIDENTIFIER
    fk = next(s for s in up.splitlines() if "FOREIGN KEY" in s)
    assert "REFERENCES [users] ([id])" in fk and "ON DELETE CASCADE" in fk
    resolved = resolve_fk_physical(sj.load(_schema()), "sqlserver", DEFAULT_REGISTRY)
    assert resolved["fld_ouser0001"]["type"] == "uniqueidentifier"


def test_sqlserver_postgres_and_mysql_differ_only_in_dialect():
    target = sj.load(_schema())
    ops = diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}), target).op_dicts()
    pg = "\n".join(emit_sql(ops, target, driver="postgres").up_statements())
    my = "\n".join(emit_sql(ops, target, driver="mysql").up_statements())
    assert '"id" uuid NOT NULL' in pg                       # uuid is native on Postgres
    assert "`id` char(36) NOT NULL" in my                   # CHAR(36) on MySQL
    assert "[id] uniqueidentifier NOT NULL" in _sqlserver_up()  # UNIQUEIDENTIFIER on SQL Server


def test_sqlserver_emit_is_deterministic():
    assert _sqlserver_up() == _sqlserver_up()


# --- import: reverse type map (native MSSQL column → semantic), no server needed ------------------


def _sqlserver_introspected() -> IntrospectedSchema:
    """What SQL Server introspection returns after the emitted DDL is applied (hand-built)."""
    return IntrospectedSchema(
        tables=[
            IntrospectedTable(name="orders", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="uniqueidentifier", nullable=False),
                IntrospectedColumn(name="user_id", data_type="uniqueidentifier", nullable=False),
                IntrospectedColumn(name="total", data_type="decimal",
                                   numeric_precision=12, numeric_scale=2, nullable=False),
            ]),
            IntrospectedTable(name="users", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="uniqueidentifier", nullable=False),
                IntrospectedColumn(name="email", data_type="nvarchar", char_max_length=255, nullable=False),
                IntrospectedColumn(name="is_active", data_type="bit", nullable=False),
                IntrospectedColumn(name="hits", data_type="int", nullable=False, is_identity=True),
                IntrospectedColumn(name="created_at", data_type="datetime2", nullable=False),
            ]),
        ],
        foreign_keys=[IntrospectedForeignKey(name="orders_user_id_fkey", table="orders",
                                             columns=["user_id"], ref_table="users", ref_columns=["id"],
                                             on_delete="cascade")],
    )


def test_sqlserver_reverse_map_recovers_semantic_types():
    built = build_schema_json(_sqlserver_introspected(), name="shop", driver="sqlserver")
    schema = built["schema_json"]
    assert schema["meta"]["databaseType"] == "sqlserver"
    fields = {(t["name"], f["name"]): f for t in schema["logical"]["tables"] for f in t["fields"]}
    assert fields[("users", "id")]["semanticType"] == "uuid"          # UNIQUEIDENTIFIER → uuid
    assert fields[("orders", "user_id")]["semanticType"] == "uuid"    # the FK target is uuid too
    assert fields[("users", "is_active")]["semanticType"] == "boolean"  # BIT → boolean
    assert fields[("users", "created_at")]["semanticType"] == "datetime"  # DATETIME2 → datetime
    assert fields[("users", "hits")].get("autoIncrement") is True       # IDENTITY → integer key
    # No spurious physical overrides: UNIQUEIDENTIFIER/BIT/DATETIME2 ARE the SQL Server defaults.
    assert "overrides" not in fields[("users", "id")]
    assert "overrides" not in fields[("users", "is_active")]
    assert "overrides" not in fields[("users", "created_at")]


def test_sqlserver_import_round_trips_to_uniqueidentifier_fk():
    """import (reverse map) → emit (forward map) reproduces the UNIQUEIDENTIFIER FK — the closed loop."""
    built = build_schema_json(_sqlserver_introspected(), name="shop", driver="sqlserver")["schema_json"]
    target = sj.load(built, validate=False)
    ops = diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}), target).op_dicts()
    up = "\n".join(emit_sql(ops, target, driver="sqlserver").up_statements())
    assert "[user_id] uniqueidentifier NOT NULL" in up


def test_sqlserver_import_is_deterministic():
    import json
    a = build_schema_json(_sqlserver_introspected(), name="shop", driver="sqlserver")["schema_json"]
    b = build_schema_json(_sqlserver_introspected(), name="shop", driver="sqlserver")["schema_json"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
