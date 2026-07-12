"""Conformance kit — Multi-driver milestone: MySQL live gate (spec §5, mandatory).

As Milestone 1 taught, a snapshot is not proof — the MySQL driver must close the same loop against a
**real MySQL/MariaDB** that Postgres does. These tests are opt-in (set ``VDB_TEST_MYSQL_DSN``, e.g.
``mysql://root:pw@127.0.0.1:3306/vdb_shadow``) but must pass once on a real server to count as proven:

  * **round-trip** — design → emit MySQL DDL → apply on real MySQL → import → equivalent schema;
  * **the FK lesson on MySQL** — a uuid PK becomes ``CHAR(36)`` and the FK column is the same
    ``CHAR(36)``, and the constraint is actually created (no type-mismatch error);
  * **file import** — a real MySQL dump (backticks, ``ENGINE=InnoDB``, ``AUTO_INCREMENT``) imports.

Postgres is unaffected — its gate (:mod:`tests.milestones.test_m2_brownfield`) stays green unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.drift import three_way_drift
from app.core.drivers import get_driver
from app.core.importer import build_schema_json, import_sql_via_shadow
from app.core.sql_emitter import emit_sql

pytestmark = pytest.mark.conformance


def _reference_schema() -> dict:
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "mysql", "defaultDriver": "mysql"},
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
                    # A uuid-keyed FK column round-trips as its physical type (uuid) — the FK-ness is
                    # carried by the relation below (foreignKeyFieldId), not the column's semantic type.
                    # This matches the SQL Server gate (test_m6) and the importer's "FK lesson" design.
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
    dsn = os.getenv("VDB_TEST_MYSQL_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_MYSQL_DSN to run the live-MySQL tests")
    if importlib.util.find_spec("pymysql") is None and importlib.util.find_spec("mysql.connector") is None:
        pytest.skip("no MySQL driver installed (PyMySQL / mysql-connector-python)")
    return dsn


def _up(designed: dict) -> list[str]:
    target = sj.load(designed)
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    return [s for s in emit_sql(diff(empty, target).op_dicts(), target, driver="mysql").up_statements()
            if not s.strip().startswith("--")]


@pytest.mark.live_mysql
def test_live_round_trip_emit_apply_import_is_equivalent():
    """design → emit MySQL DDL → apply on a real MySQL → import → structurally equivalent."""
    dsn = _dsn_or_skip()
    drv = get_driver("mysql")
    drv.reset(dsn)
    try:
        drv.apply_sql(dsn, _up(_reference_schema()))
        imported = build_schema_json(drv.introspect(dsn), driver="mysql")["schema_json"]
        assert _fingerprint(_reference_schema()) == _fingerprint(imported)
    finally:
        drv.reset(dsn)


@pytest.mark.live_mysql
def test_live_uuid_fk_is_char36_and_constraint_is_created():
    """The exact M1 bug, now on MySQL: a uuid FK must be CHAR(36) and the constraint must build."""
    dsn = _dsn_or_skip()
    drv = get_driver("mysql")
    drv.reset(dsn)
    try:
        drv.apply_sql(dsn, _up(_reference_schema()))  # raises if MySQL rejects the FK type
        introspected = drv.introspect(dsn)
        cols = {(t.name, c.name): c for t in introspected.tables for c in t.columns}
        assert cols[("users", "id")].column_type.lower().startswith("char(36)")
        assert cols[("orders", "user_id")].column_type.lower().startswith("char(36)")  # FK == PK type
        assert any(fk.table == "orders" and fk.ref_table == "users" for fk in introspected.foreign_keys)
    finally:
        drv.reset(dsn)


@pytest.mark.live_mysql
def test_live_file_import_of_a_real_mysql_dump():
    """A real MySQL dump (backticks, AUTO_INCREMENT, ENGINE=InnoDB) imports via the shadow DB (§2)."""
    dsn = _dsn_or_skip()
    dump = """
        CREATE TABLE `users` (
            `id` int(11) NOT NULL AUTO_INCREMENT,
            `email` varchar(255) NOT NULL,
            `is_active` tinyint(1) NOT NULL DEFAULT 1,
            PRIMARY KEY (`id`)
        ) ENGINE=InnoDB;
        CREATE TABLE `posts` (
            `id` int(11) NOT NULL AUTO_INCREMENT,
            `user_id` int(11) NOT NULL,
            `title` varchar(200) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `posts_user_fk` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
        ) ENGINE=InnoDB;
    """
    result = import_sql_via_shadow(dump, dsn, name="blog", driver="mysql")
    schema = result["schema_json"]
    assert {t["name"] for t in schema["logical"]["tables"]} == {"users", "posts"}
    s = sj.load(schema, validate=False)
    users = next(t for t in s.logical.tables if t.name == "users")
    active = next(f for f in users.fields if f.name == "is_active")
    assert active.semantic_type == "boolean"   # TINYINT(1) → boolean
    idf = next(f for f in users.fields if f.name == "id")
    assert idf.auto_increment is True          # AUTO_INCREMENT recognised
    assert any(r.type in {"one_to_many", "one_to_one"} for r in s.logical.relations)  # FK → relation
    # Deterministic: importing the same dump twice is byte-identical.
    again = import_sql_via_shadow(dump, dsn, name="blog", driver="mysql")["schema_json"]
    assert json.dumps(schema, sort_keys=True) == json.dumps(again, sort_keys=True)


@pytest.mark.live_mysql
def test_live_file_import_of_a_phpmyadmin_dump():
    """A real **phpMyAdmin** dump — comments before every statement, ``/*! */`` pragmas, ``START
    TRANSACTION``/``COMMIT``, a ``;`` inside a string — imports cleanly (the import-bug fix). The old
    splitter glued the comments to each ``CREATE TABLE`` and the apply step skipped them, so no table
    was created; this guards that regression on a real server."""
    dsn = _dsn_or_skip()
    dump = """-- phpMyAdmin SQL Dump
-- version 5.2.1
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
/*!40101 SET NAMES utf8mb4 */;

--
-- Table structure for table `users`
--
CREATE TABLE `users` (
  `id` bigint(20) UNSIGNED NOT NULL,
  `email` varchar(255) NOT NULL,
  `bio` text DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

--
-- Dumping data for table `users`
--
INSERT INTO `users` (`id`, `email`, `bio`) VALUES
(1, 'a@example.com', 'note; with a semicolon');

-- --------------------------------------------------------

--
-- Table structure for table `orders`
--
CREATE TABLE `orders` (
  `id` bigint(20) UNSIGNED NOT NULL,
  `user_id` bigint(20) UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

--
-- Indexes for dumped tables
--
ALTER TABLE `users` ADD PRIMARY KEY (`id`);
ALTER TABLE `orders` ADD PRIMARY KEY (`id`), ADD KEY `orders_user_id_foreign` (`user_id`);

ALTER TABLE `users` MODIFY `id` bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT;
ALTER TABLE `orders` MODIFY `id` bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT;

--
-- Constraints for dumped tables
--
ALTER TABLE `orders`
  ADD CONSTRAINT `orders_user_id_foreign` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE;
COMMIT;
"""
    result = import_sql_via_shadow(dump, dsn, name="hosh", driver="mysql")
    s = sj.load(result["schema_json"], validate=False)
    assert {t.name for t in s.logical.tables} == {"users", "orders"}      # both CREATEs survived
    users = next(t for t in s.logical.tables if t.name == "users")
    assert any(f.name == "id" and f.is_primary_key for f in users.fields)  # the ALTER PK applied
    assert any(f.name == "id" and f.auto_increment for f in users.fields)  # AUTO_INCREMENT applied
    assert any(r.to_table_id and r.from_table_id for r in s.logical.relations)  # FK rebuilt


def _drift_design() -> dict:
    """Leg A (designed): users(id uuid, email, phone) + a not-yet-migrated drafts table. The uuid PK is
    CHAR(36) on MySQL — the FK lesson the drift comparison must respect (no spurious type drift)."""
    return sj.load({
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "mysql", "defaultDriver": "mysql"},
        "logical": {"tables": [
            {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                 "isPrimaryKey": True, "nullable": False},
                {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                {"id": "fld_uphone001", "name": "phone", "semanticType": "string", "nullable": True},
            ]},
            {"id": "tbl_drafts001", "name": "drafts", "kind": "normal", "fields": [
                {"id": "fld_did000001", "name": "id", "semanticType": "uuid",
                 "isPrimaryKey": True, "nullable": False},
            ]},
        ]},
    }, validate=False)


@pytest.mark.live_mysql
def test_live_mysql_three_way_drift_reports_categories_and_respects_fk_type():
    """drift on a **real MySQL** (multi-driver §2): the same six-category classification Postgres gives,
    via the driver pattern (introspect/apply through the MySQL driver, not a separate code path), and
    the FK lesson — a uuid PK that is CHAR(36) on MySQL is *not* mis-reported as type drift.

    Legs are built by applying DDL and introspecting (resetting the shadow DB between), so a single
    dedicated MySQL database is enough; it is a throwaway shadow, never a real one (the round-trip drops
    its tables)."""
    dsn = _dsn_or_skip()
    drv = get_driver("mysql")
    try:
        # Leg B (migrations): users(id, email, phone).
        drv.reset(dsn)
        drv.apply_sql(dsn, ["CREATE TABLE `users` (`id` CHAR(36) NOT NULL, `email` VARCHAR(255) NOT NULL, "
                            "`phone` VARCHAR(255) NULL, PRIMARY KEY (`id`)) ENGINE=InnoDB;"])
        migrations = sj.load(build_schema_json(drv.introspect(dsn), driver="mysql")["schema_json"], validate=False)
        # Leg C (live): phone never applied; a manual `hotfix` column was added straight on prod instead.
        drv.reset(dsn)
        drv.apply_sql(dsn, ["CREATE TABLE `users` (`id` CHAR(36) NOT NULL, `email` VARCHAR(255) NOT NULL, "
                            "`hotfix` VARCHAR(255) NULL, PRIMARY KEY (`id`)) ENGINE=InnoDB;"])
        live = sj.load(build_schema_json(drv.introspect(dsn), driver="mysql")["schema_json"], validate=False)

        report = three_way_drift(_drift_design(), migrations, live, driver="mysql")
        by_entity = {d.entity: d.category for d in report.drift}
        assert by_entity.get("users.phone") == "migration_not_applied"
        assert by_entity.get("users.hotfix") == "manual_prod_change"
        assert by_entity.get("drafts") == "design_ahead_of_code"
        assert report.exit_code == 1
        # The FK lesson on MySQL: the uuid PK (CHAR(36) on MySQL) is synced across all legs, never drift.
        assert "users.id" not in by_entity
    finally:
        drv.reset(dsn)
