"""Unit tests — SQL Emitter (Milestone 1 §7).

Greenfield emit (empty → canonical) creates tables before their foreign keys, builds indexes
concurrently, renders physical types from the Type System, marks destructive ops irreversible +
backup-required, produces reverse-ordered rollback SQL, rejects unsupported drivers, and is
deterministic.
"""

from __future__ import annotations

import pytest

from app.core import schema_json as sj
from app.core.diff import diff
from app.core.sql_emitter import emit_sql

from .factory import canonical_schema


def _greenfield_ops(target_dict):
    empty = sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}})
    target = sj.load(target_dict)
    return diff(empty, target).op_dicts(), target


def test_greenfield_creates_tables_then_fks_then_indexes():
    ops, target = _greenfield_ops(canonical_schema())
    up = emit_sql(ops, target).up_statements()
    joined = "\n".join(up)
    assert 'CREATE TABLE "users"' in joined
    assert 'CREATE TABLE "orders"' in joined
    # two-phase: every CREATE TABLE precedes the FK that references it.
    fk_idx = next(i for i, s in enumerate(up) if "ADD CONSTRAINT" in s and "FOREIGN KEY" in s)
    create_idxs = [i for i, s in enumerate(up) if s.startswith("CREATE TABLE")]
    assert all(c < fk_idx for c in create_idxs)


def test_physical_types_come_from_type_system():
    ops, target = _greenfield_ops(canonical_schema())
    joined = "\n".join(emit_sql(ops, target).up_statements())
    assert '"email" varchar(255) NOT NULL' in joined  # email semantic → varchar(255)
    assert '"total" numeric(12,2) NOT NULL' in joined  # money → numeric(12,2) on postgres
    assert '"id" uuid NOT NULL' in joined


def test_index_is_built_concurrently_and_unique():
    ops, target = _greenfield_ops(canonical_schema())
    up = emit_sql(ops, target).up_statements()
    assert any("CREATE UNIQUE INDEX CONCURRENTLY" in s and '"users"' in s for s in up)


def test_foreign_key_references_pk_with_on_delete():
    ops, target = _greenfield_ops(canonical_schema())
    up = emit_sql(ops, target).up_statements()
    fk = next(s for s in up if "FOREIGN KEY" in s)
    assert 'REFERENCES "users" ("id")' in fk and "ON DELETE CASCADE" in fk


def test_non_ddl_ops_are_skipped():
    ops, target = _greenfield_ops(canonical_schema())
    script = emit_sql(ops, target)
    assert "add_business_rule" in script.skipped  # semantic layer has no DDL


def test_drop_table_is_irreversible_and_requires_backup():
    base = sj.load(canonical_schema())
    after_dict = canonical_schema()
    after_dict["logical"]["tables"] = [after_dict["logical"]["tables"][0]]
    after_dict["logical"]["relations"] = []
    after_dict["physical"] = {"indexes": after_dict["physical"]["indexes"]}
    after_dict["semantic"] = {}
    after = sj.load(after_dict, validate=False)
    ops = diff(base, after).op_dicts()
    script = emit_sql(ops, after)
    drop = next(s for s in script.steps if s.op == "drop_table")
    assert drop.reversible is False and drop.requires_backup is True
    assert script.requires_backup is True
    assert any("DROP TABLE" in s for s in drop.up)


def test_rollback_is_reverse_ordered():
    ops, target = _greenfield_ops(canonical_schema())
    script = emit_sql(ops, target)
    down = script.down_statements()
    # the FK (applied last) is dropped first; tables (applied first) are dropped last.
    assert "DROP CONSTRAINT" in down[0]
    assert down[-1].startswith("DROP TABLE")


def test_unsupported_driver_raises():
    ops, target = _greenfield_ops(canonical_schema())
    with pytest.raises(ValueError, match="postgres"):
        emit_sql(ops, target, driver="mysql")


def test_emitter_is_deterministic():
    ops, target = _greenfield_ops(canonical_schema())
    a = emit_sql(ops, target).model_dump()
    b = emit_sql(ops, target).model_dump()
    assert a == b
