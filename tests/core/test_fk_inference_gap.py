"""Regression for bug #6 residual — foreign-key relations dropped/corrupted during introspection.

Two root causes, both in the driver introspection layer (not the pure builder, which these tests also
pin as already-correct):

1. **Name-collision accumulation** (all drivers): the per-driver ``introspect`` accumulated foreign
   keys in a dict keyed by *constraint name alone*. Postgres allows two tables to share an FK
   constraint name (names are unique per table, not per schema), so two same-named FKs merged into one
   object — one relation lost, the other corrupted. Fixed by keying on ``(table, constraint)``.
2. **Postgres composite cartesian** (postgres only): the old ``information_schema`` query joined FK and
   referenced columns on the constraint name only, Cartesian-exploding a composite FK's columns. Fixed
   by a ``pg_catalog`` query that pairs columns by position (``unnest(conkey, confkey) WITH ORDINALITY``).

The always-run tests use the pure builder and a fake DB cursor; the ``live_*`` tests prove it against a
real server (set ``VDB_TEST_{POSTGRES,MYSQL,SQLSERVER}_DSN``).
"""

from __future__ import annotations

import os

import pytest

from app.core.drivers import get_driver
from app.core.drivers.base import (
    IntrospectedColumn, IntrospectedForeignKey, IntrospectedSchema, IntrospectedTable,
)
from app.core.importer import build_schema_json


def _col(name: str, pk: bool = False) -> IntrospectedColumn:
    return IntrospectedColumn(name=name, data_type="uuid", udt_name="uuid", nullable=not pk)


def _tbl(name: str, cols: list[str], pk: str) -> IntrospectedTable:
    return IntrospectedTable(name=name, columns=[_col(c, c == pk) for c in cols], primary_key=[pk])


def _relations(schema: IntrospectedSchema) -> list[dict]:
    return build_schema_json(schema, driver="postgres")["schema_json"]["logical"]["relations"]


# --------------------------------------------------------------------------------------------------
# Pure builder — already correct; pinned so a future change can't regress the shapes the drivers feed.
# --------------------------------------------------------------------------------------------------

def test_builder_keeps_two_same_named_fks_on_different_tables_distinct():
    """The crux of the bug lived in the driver, but the builder must also emit both relations when it
    receives two distinct FKs that happen to share a name (orders.fk_owner, invoices.fk_owner)."""
    schema = IntrospectedSchema(
        tables=[_tbl("users", ["id"], "id"), _tbl("companies", ["id"], "id"),
                _tbl("orders", ["id", "user_id"], "id"), _tbl("invoices", ["id", "company_id"], "id")],
        foreign_keys=[
            IntrospectedForeignKey(name="fk_owner", table="orders", columns=["user_id"], ref_table="users", ref_columns=["id"]),
            IntrospectedForeignKey(name="fk_owner", table="invoices", columns=["company_id"], ref_table="companies", ref_columns=["id"]),
        ],
    )
    rels = _relations(schema)
    assert len(rels) == 2
    assert len({r["id"] for r in rels}) == 2  # distinct stable ids (AD-1 preserved)


def test_builder_handles_self_referencing_fk():
    schema = IntrospectedSchema(
        tables=[_tbl("employees", ["id", "manager_id"], "id")],
        foreign_keys=[IntrospectedForeignKey(name="fk_mgr", table="employees", columns=["manager_id"],
                                             ref_table="employees", ref_columns=["id"])],
    )
    rels = _relations(schema)
    assert len(rels) == 1
    assert rels[0]["fromTableId"] == rels[0]["toTableId"]  # self relation


def test_builder_handles_composite_fk():
    schema = IntrospectedSchema(
        tables=[_tbl("order_lines", ["order_id", "seq"], "order_id"),
                _tbl("shipments", ["id", "order_id", "seq"], "id")],
        foreign_keys=[IntrospectedForeignKey(name="fk_line", table="shipments", columns=["order_id", "seq"],
                                             ref_table="order_lines", ref_columns=["order_id", "seq"])],
    )
    assert len(_relations(schema)) == 1


def test_builder_is_order_independent_ref_table_listed_after():
    """Referencing table listed before its target — the relation must still resolve (table ids are all
    minted before relations are built)."""
    schema = IntrospectedSchema(
        tables=[_tbl("orders", ["id", "user_id"], "id"), _tbl("users", ["id"], "id")],  # orders first
        foreign_keys=[IntrospectedForeignKey(name="fk_u", table="orders", columns=["user_id"],
                                             ref_table="users", ref_columns=["id"])],
    )
    assert len(_relations(schema)) == 1


# --------------------------------------------------------------------------------------------------
# Driver accumulation — the actual fix. Fake cursor feeds the FK rows the DB would return; no server.
# --------------------------------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fk_rows: list[tuple]):
        self._fk_rows = fk_rows
        self._last = ""

    def execute(self, sql: str, params=None):
        self._last = sql

    def fetchall(self):
        return self._fk_rows if "pg_constraint" in self._last else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fk_rows):
        self._fk_rows = fk_rows

    def cursor(self):
        return _FakeCursor(self._fk_rows)

    def close(self):
        pass


def test_postgres_introspect_does_not_merge_same_named_fks(monkeypatch):
    """Two constraints both named ``fk_owner`` on different tables must survive as two FK objects with
    the correct per-table columns — not one merged object (the missing-relation bug)."""
    import app.core.drivers.postgres as pg
    # (constraint_name, table, column, ordinal, ref_table, ref_column, delete_rule, update_rule)
    fk_rows = [
        ("fk_owner", "orders", "user_id", 1, "users", "id", None, None),
        ("fk_owner", "invoices", "company_id", 1, "companies", "id", None, None),
    ]
    monkeypatch.setattr(pg, "connect", lambda dsn: _FakeConn(fk_rows))
    fks = pg.introspect("postgresql://x").foreign_keys
    assert len(fks) == 2
    by_table = {f.table: f for f in fks}
    assert by_table["orders"].columns == ["user_id"] and by_table["orders"].ref_table == "users"
    assert by_table["invoices"].columns == ["company_id"] and by_table["invoices"].ref_table == "companies"


def test_postgres_introspect_composite_fk_columns_are_paired_not_cartesian(monkeypatch):
    """A composite FK arrives as one row per column (paired by ordinal); the columns must accumulate in
    order — no Cartesian duplication like ['order_id','order_id','seq','seq']."""
    import app.core.drivers.postgres as pg
    fk_rows = [
        ("fk_line", "shipments", "order_id", 1, "order_lines", "order_id", None, None),
        ("fk_line", "shipments", "seq", 2, "order_lines", "seq", None, None),
    ]
    monkeypatch.setattr(pg, "connect", lambda dsn: _FakeConn(fk_rows))
    fks = pg.introspect("postgresql://x").foreign_keys
    assert len(fks) == 1
    assert fks[0].columns == ["order_id", "seq"]
    assert fks[0].ref_columns == ["order_id", "seq"]


# --------------------------------------------------------------------------------------------------
# Live gates — prove the fix end-to-end against real servers (opt-in via DSN env).
# --------------------------------------------------------------------------------------------------

pytestmark = pytest.mark.conformance

_PG_DDL = [
    "DROP TABLE IF EXISTS shipments, order_lines, invoices, orders, employees, companies, users CASCADE",
    "CREATE TABLE users (id uuid PRIMARY KEY)",
    "CREATE TABLE companies (id uuid PRIMARY KEY)",
    "CREATE TABLE orders (id uuid PRIMARY KEY, user_id uuid, CONSTRAINT fk_owner FOREIGN KEY (user_id) REFERENCES users(id))",
    "CREATE TABLE invoices (id uuid PRIMARY KEY, company_id uuid, CONSTRAINT fk_owner FOREIGN KEY (company_id) REFERENCES companies(id))",
    "CREATE TABLE employees (id uuid PRIMARY KEY, manager_id uuid, CONSTRAINT fk_mgr FOREIGN KEY (manager_id) REFERENCES employees(id))",
    "CREATE TABLE order_lines (order_id uuid, seq int, PRIMARY KEY (order_id, seq))",
    "CREATE TABLE shipments (id uuid PRIMARY KEY, order_id uuid, seq int, CONSTRAINT fk_line FOREIGN KEY (order_id, seq) REFERENCES order_lines(order_id, seq))",
]


@pytest.mark.live_postgres
def test_live_postgres_same_name_composite_and_self_ref_all_survive():
    dsn = os.getenv("VDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set VDB_TEST_POSTGRES_DSN")
    drv = get_driver("postgres")
    drv.reset(dsn)  # isolate: drop everything first so the count reflects only this schema
    drv.apply_sql(dsn, _PG_DDL)
    try:
        intro = drv.introspect(dsn)
        rels = build_schema_json(intro, driver="postgres")["schema_json"]["logical"]["relations"]
        # 4 relations: orders->users, invoices->companies (same FK name!), employees->employees, composite.
        assert len(rels) == 4
        line = next(f for f in intro.foreign_keys if f.name == "fk_line")
        assert line.columns == ["order_id", "seq"] and line.ref_columns == ["order_id", "seq"]
        owners = [f for f in intro.foreign_keys if f.name == "fk_owner"]
        assert {f.table for f in owners} == {"orders", "invoices"}  # both survived, not merged
    finally:
        drv.apply_sql(dsn, ["DROP TABLE IF EXISTS shipments, order_lines, invoices, orders, employees, companies, users CASCADE"])


def _live_dialect(driver: str, env: str, ddl: list[str], drops: list[str], expected: int):
    dsn = os.getenv(env)
    if not dsn:
        pytest.skip(f"set {env}")
    drv = get_driver(driver)
    drv.reset(dsn)  # isolate: drop everything first so the count reflects only this schema
    drv.apply_sql(dsn, ddl)
    try:
        intro = drv.introspect(dsn)
        rels = build_schema_json(intro, driver=driver)["schema_json"]["logical"]["relations"]
        assert len(rels) == expected
        line = next(f for f in intro.foreign_keys if "line" in f.name)
        assert len(line.columns) == 2  # composite kept intact
        assert any(f.table == f.ref_table for f in intro.foreign_keys)  # self-ref present
    finally:
        for s in drops:
            try:
                drv.apply_sql(dsn, [s])
            except Exception:
                pass


@pytest.mark.live_mysql
def test_live_mysql_composite_and_self_ref_survive():
    ddl = [
        "CREATE TABLE users (id char(36) PRIMARY KEY)",
        "CREATE TABLE orders (id char(36) PRIMARY KEY, user_id char(36), CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id))",
        "CREATE TABLE employees (id char(36) PRIMARY KEY, manager_id char(36), CONSTRAINT fk_emp_mgr FOREIGN KEY (manager_id) REFERENCES employees(id))",
        "CREATE TABLE order_lines (order_id char(36), seq int, PRIMARY KEY (order_id, seq))",
        "CREATE TABLE shipments (id char(36) PRIMARY KEY, order_id char(36), seq int, CONSTRAINT fk_ship_line FOREIGN KEY (order_id, seq) REFERENCES order_lines(order_id, seq))",
    ]
    drops = ["SET FOREIGN_KEY_CHECKS=0", "DROP TABLE IF EXISTS shipments, order_lines, employees, orders, users", "SET FOREIGN_KEY_CHECKS=1"]
    _live_dialect("mysql", "VDB_TEST_MYSQL_DSN", ddl, drops, 3)


@pytest.mark.live_sqlserver
def test_live_sqlserver_composite_and_self_ref_survive():
    ddl = [
        "CREATE TABLE users (id uniqueidentifier PRIMARY KEY)",
        "CREATE TABLE orders (id uniqueidentifier PRIMARY KEY, user_id uniqueidentifier, CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id))",
        "CREATE TABLE employees (id uniqueidentifier PRIMARY KEY, manager_id uniqueidentifier, CONSTRAINT fk_emp_mgr FOREIGN KEY (manager_id) REFERENCES employees(id))",
        "CREATE TABLE order_lines (order_id uniqueidentifier, seq int, CONSTRAINT pk_ol PRIMARY KEY (order_id, seq))",
        "CREATE TABLE shipments (id uniqueidentifier PRIMARY KEY, order_id uniqueidentifier, seq int, CONSTRAINT fk_ship_line FOREIGN KEY (order_id, seq) REFERENCES order_lines(order_id, seq))",
    ]
    drops = [f"IF OBJECT_ID('{t}') IS NOT NULL DROP TABLE {t}" for t in ("shipments", "order_lines", "orders", "employees", "users")]
    _live_dialect("sqlserver", "VDB_TEST_SQLSERVER_DSN", ddl, drops, 3)
