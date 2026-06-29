"""Conformance kit — MySQL driver + the driver pattern (multi-driver milestone).

Two things are proven here without a server (the live gate in
:mod:`tests.milestones.test_m5_mysql` proves them against real MySQL):

* the **emit** side — MySQL DDL is correct dialect (backticks, ``AUTO_INCREMENT``, ``ENGINE=InnoDB``,
  ``TINYINT(1)``, no ``CONCURRENTLY``) and, critically, the **FK lesson** holds on MySQL: a uuid PK is
  ``CHAR(36)`` and its foreign-key column is the *same* ``CHAR(36)`` (spec §1);
* the **import** side — the reverse type map turns native MySQL columns back into semantic types
  (``CHAR(36)`` → uuid, ``TINYINT(1)`` → boolean, ``AUTO_INCREMENT`` → integer key) so the
  emit↔import round-trip closes (spec §1/§2).

The driver pattern itself (registry, aliases, the still-unsupported guard) is exercised too — the
point of the milestone is that a third database is a new module, not a Core change (spec §0).
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
    """users (uuid PK, email, is_active boolean, serial counter) ← orders.user_id (uuid FK)."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "mysql", "defaultDriver": "mysql"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                    {"id": "fld_uactive01", "name": "is_active", "semanticType": "boolean", "nullable": False},
                    {"id": "fld_uhits0001", "name": "hits", "semanticType": "integer",
                     "nullable": False, "autoIncrement": True},
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


def _mysql_up() -> str:
    target = sj.load(_schema())
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    ops = diff(empty, target).op_dicts()
    return "\n".join(emit_sql(ops, target, driver="mysql").up_statements())


# --- the driver pattern ---------------------------------------------------------------------------


def test_registry_resolves_drivers_and_aliases():
    assert get_dialect("postgres").quote_char == '"'
    assert get_dialect("mysql").quote_char == "`"
    assert get_driver("mariadb").name == "mysql"  # MariaDB speaks the MySQL dialect


def test_unsupported_driver_raises():
    import pytest
    with pytest.raises(ValueError, match="sqlite"):
        get_driver("sqlite")


# --- emit: MySQL dialect --------------------------------------------------------------------------


def test_mysql_dialect_basics():
    up = _mysql_up()
    assert "CREATE TABLE `users`" in up and "ENGINE=InnoDB" in up
    assert "`email` varchar(255) NOT NULL" in up
    assert "`is_active` tinyint(1) NOT NULL" in up  # boolean → TINYINT(1)
    assert "`total` decimal(12,2) NOT NULL" in up   # money → DECIMAL
    assert "`hits` integer NOT NULL AUTO_INCREMENT" in up  # serial → AUTO_INCREMENT keyword
    assert "CREATE UNIQUE INDEX `users_email_uniq` ON `users`" in up
    assert "CONCURRENTLY" not in up  # MySQL has no concurrent index build


def test_mysql_fk_column_matches_uuid_pk_as_char36():
    """The FK lesson, on MySQL: a uuid PK is CHAR(36) and the FK column is the SAME CHAR(36)."""
    up = _mysql_up()
    assert "`id` char(36) NOT NULL" in up        # uuid PK → CHAR(36)
    assert "`user_id` char(36) NOT NULL" in up   # uuid FK inherits exactly CHAR(36)
    fk = next(s for s in up.splitlines() if "FOREIGN KEY" in s)
    assert "REFERENCES `users` (`id`)" in fk and "ON DELETE CASCADE" in fk
    # And the Type System resolves the FK physical the same way the emitter renders it.
    resolved = resolve_fk_physical(sj.load(_schema()), "mysql", DEFAULT_REGISTRY)
    assert resolved["fld_ouser0001"]["type"] == "char" and resolved["fld_ouser0001"]["length"] == 36


def test_mysql_and_postgres_differ_only_in_dialect():
    target = sj.load(_schema())
    ops = diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}), target).op_dicts()
    pg = "\n".join(emit_sql(ops, target, driver="postgres").up_statements())
    assert '"id" uuid NOT NULL' in pg          # uuid is native on Postgres
    assert '"user_id" uuid NOT NULL' in pg      # FK follows the PK on Postgres too
    assert "ENGINE=InnoDB" not in pg


def test_mysql_emit_is_deterministic():
    assert _mysql_up() == _mysql_up()


# --- import: reverse type map (native MySQL column → semantic), no server needed -------------------


def _mysql_introspected() -> IntrospectedSchema:
    """What MySQL introspection returns after the emitted DDL is applied (hand-built, deterministic)."""
    return IntrospectedSchema(
        tables=[
            IntrospectedTable(name="orders", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="char", column_type="char(36)",
                                   char_max_length=36, nullable=False),
                IntrospectedColumn(name="user_id", data_type="char", column_type="char(36)",
                                   char_max_length=36, nullable=False),
                IntrospectedColumn(name="total", data_type="decimal", column_type="decimal(12,2)",
                                   numeric_precision=12, numeric_scale=2, nullable=False),
            ]),
            IntrospectedTable(name="users", primary_key=["id"], columns=[
                IntrospectedColumn(name="id", data_type="char", column_type="char(36)",
                                   char_max_length=36, nullable=False),
                IntrospectedColumn(name="email", data_type="varchar", column_type="varchar(255)",
                                   char_max_length=255, nullable=False),
                IntrospectedColumn(name="is_active", data_type="tinyint", column_type="tinyint(1)", nullable=False),
                IntrospectedColumn(name="hits", data_type="bigint", column_type="bigint",
                                   nullable=False, is_identity=True),
            ]),
        ],
        foreign_keys=[IntrospectedForeignKey(name="orders_user_id_fkey", table="orders",
                                             columns=["user_id"], ref_table="users", ref_columns=["id"],
                                             on_delete="cascade")],
    )


def test_mysql_reverse_map_recovers_semantic_types():
    built = build_schema_json(_mysql_introspected(), name="shop", driver="mysql")
    schema = built["schema_json"]
    assert schema["meta"]["databaseType"] == "mysql"
    fields = {(t["name"], f["name"]): f for t in schema["logical"]["tables"] for f in t["fields"]}
    assert fields[("users", "id")]["semanticType"] == "uuid"          # CHAR(36) → uuid
    assert fields[("orders", "user_id")]["semanticType"] == "uuid"    # the FK target is uuid too
    assert fields[("users", "is_active")]["semanticType"] == "boolean"  # TINYINT(1) → boolean
    assert fields[("users", "hits")].get("autoIncrement") is True       # AUTO_INCREMENT → integer key
    # No spurious physical overrides: CHAR(36)/TINYINT(1) ARE uuid/boolean's MySQL defaults.
    assert "overrides" not in fields[("users", "id")]
    assert "overrides" not in fields[("users", "is_active")]


def test_mysql_import_round_trips_to_char36_fk():
    """import (reverse map) → emit (forward map) reproduces the CHAR(36) FK — the closed loop (spec §1)."""
    built = build_schema_json(_mysql_introspected(), name="shop", driver="mysql")["schema_json"]
    target = sj.load(built, validate=False)
    ops = diff(sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}), target).op_dicts()
    up = "\n".join(emit_sql(ops, target, driver="mysql").up_statements())
    assert "`user_id` char(36) NOT NULL" in up


def test_mysql_import_is_deterministic():
    a = build_schema_json(_mysql_introspected(), name="shop", driver="mysql")["schema_json"]
    b = build_schema_json(_mysql_introspected(), name="shop", driver="mysql")["schema_json"]
    import json
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
