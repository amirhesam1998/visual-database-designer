"""SQL Emitter — the last mile that turns a migration plan into executable DDL (Milestone 1 §7).

The Risk Analyzer produces a *safe plan* (ordered, risk-graded operations) but no runnable SQL.
This module closes that gap: it consumes the same id-based operation list the Risk Analyzer does
and emits ordered, per-driver SQL statements with both ``up`` and ``down`` (rollback) where the
operation is reversible.

Two deliberate constraints (spec §7 / §12):

* **The dialect is a driver, not a branch.** Per-driver syntax (quoting, ``CREATE INDEX
  CONCURRENTLY`` vs plain, ``serial`` vs ``AUTO_INCREMENT``, ``ENGINE=InnoDB``) lives in
  :class:`app.core.drivers.SqlDialect`; this module orchestrates ops and asks the dialect to render.
  Adding a database never edits the emitter (multi-driver milestone §0/§3).
* **Physical types come from the Type System** (:mod:`app.core.type_system`), never re-derived — and
  the FK rule (an FK column uses the referenced PK's type) is driver-aware there, so a uuid PK is
  ``uuid`` on Postgres and ``CHAR(36)`` on MySQL with the FK column following automatically.

Irreversible operations (``drop_table``, ``drop_column``, narrowing ``change_type``) emit a warning
comment in the ``up`` SQL and are flagged ``requires_backup`` so the handoff can surface them. The
emitter is pure and deterministic: the same (operations, schema, driver) always yields byte-identical SQL.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.core.drivers import SUPPORTED_DRIVERS, SqlDialect, get_dialect
from app.core.risk import _driver_hint
from app.core.schema_json import Field_, SchemaJson, Table
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, resolve_fk_physical

# Operations that have no physical (DDL) representation — they live in the semantic/presentation
# layers (business rules, state machines, ownership) or are app-level (string-backed enums). They
# are intentionally skipped by the emitter and reported in ``SqlScript.skipped``.
_NON_DDL_OPS = frozenset({
    "add_business_rule", "change_business_rule", "drop_business_rule",
    "add_state", "drop_state", "add_transition", "drop_transition",
    "change_state_machine", "change_table_meta", "change_semantic_type",
    "add_enum_value", "drop_enum_value", "rename_enum_value",
})


class SqlStep(BaseModel):
    op: str
    target: str | None = None
    up: list[str] = Field(default_factory=list)
    down: list[str] = Field(default_factory=list)
    reversible: bool = True
    requires_backup: bool = False
    note: str | None = None


class SqlScript(BaseModel):
    driver: str
    steps: list[SqlStep] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)

    def up_statements(self) -> list[str]:
        return [s for step in self.steps for s in step.up]

    def down_statements(self) -> list[str]:
        """Rollback statements in reverse application order (last applied is undone first)."""
        return [s for step in reversed(self.steps) for s in step.down]

    @property
    def requires_backup(self) -> bool:
        return any(step.requires_backup for step in self.steps)


# --------------------------------------------------------------------------------------------------
# Literal rendering (shared across drivers).
# --------------------------------------------------------------------------------------------------
_FUNC_DEFAULT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\s*\(.*\)$")
_RAW_DEFAULTS = {"CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "now()", "null", "NULL", "true", "false"}


def _render_default(value: Any) -> str:
    """Render a column default as a SQL literal/expression."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text in _RAW_DEFAULTS or _FUNC_DEFAULT.match(text):
        return text  # SQL function or keyword default — pass through verbatim
    return "'" + text.replace("'", "''") + "'"


# --------------------------------------------------------------------------------------------------
# Type rendering (Type System → driver type string, via the dialect).
# --------------------------------------------------------------------------------------------------
def _fk_type_overrides(schema: SchemaJson, dialect: SqlDialect, reg: TypeRegistry) -> dict[str, str]:
    """Rendered FK column types, sourced from the Type System's shared FK resolution (spec M2 §7).

    The "an FK column must use the referenced PK's physical type" rule lives in
    :func:`type_system.resolve_fk_physical` (driver-aware, shared with the importer); here we only
    render the resolved physical spec to this driver's type string.
    """
    return {fid: dialect.physical_to_type(p)
            for fid, p in resolve_fk_physical(schema, dialect.name, reg).items()}


def _column_def(field: Field_, dialect: SqlDialect, reg: TypeRegistry, *, inline_pk: bool = False,
                type_override: str | None = None) -> str:
    parts = [dialect.q(field.name), type_override or dialect.render_type(field, reg)]
    if not field.nullable:
        parts.append("NOT NULL")
    if field.default is not None:
        parts.append(f"DEFAULT {_render_default(field.default)}")
    if field.auto_increment and dialect.autoincrement_keyword:
        parts.append(dialect.autoincrement_keyword)
    if inline_pk and field.is_primary_key:
        parts.append("PRIMARY KEY")
    return " ".join(parts)


# --------------------------------------------------------------------------------------------------
# Schema lookups (operations carry ids; DDL needs names).
# --------------------------------------------------------------------------------------------------
def _index_name(table: Table, columns: list[str], *, unique: bool) -> str:
    cols = [_col_name(table, c) for c in columns]
    suffix = "uniq" if unique else "idx"
    return "_".join([table.name, *cols, suffix])


def _col_name(table: Table | None, field_id: str) -> str:
    if table is not None:
        f = table.field_by_id(field_id)
        if f is not None:
            return f.name
    return field_id  # best effort if the id can't be resolved


def _pk_column(table: Table | None) -> str:
    if table is not None:
        pks = table.primary_keys()
        if pks:
            return pks[0].name
    return "id"


# --------------------------------------------------------------------------------------------------
# Per-op DDL.
# --------------------------------------------------------------------------------------------------
def _emit_op(op: dict[str, Any], schema: SchemaJson, dialect: SqlDialect, reg: TypeRegistry,
             fk_overrides: dict[str, str]) -> SqlStep | None:
    name = op["op"]
    if name in _NON_DDL_OPS:
        return None

    table_id = op.get("tableId")
    field_id = op.get("fieldId")
    entity_id = op.get("entityId")
    table = schema.table_by_id(table_id) if table_id else None
    q = dialect.q
    hint = _driver_hint(dialect.name, name)

    if name == "add_table":
        return _emit_create_table(schema.table_by_id(table_id), dialect, reg, fk_overrides)
    if name == "drop_table":
        tname = op.get("name") or (table.name if table else table_id)
        return SqlStep(
            op=name, target=tname,
            up=[f"-- WARNING: irreversible, destroys all data in {tname}; take a backup first.",
                dialect.drop_table_sql(tname)],
            down=[f"-- cannot auto-restore dropped table {tname}; restore from backup."],
            reversible=False, requires_backup=True,
        )
    if name == "add_column":
        return _emit_add_column(table, field_id, op, dialect, reg, fk_overrides)
    if name == "drop_column":
        cname = op.get("name") or _col_name(table, field_id or "")
        tname = table.name if table else table_id
        return SqlStep(
            op=name, target=f"{tname}.{cname}",
            up=[f"-- WARNING: irreversible, drops column data in {tname}.{cname}.",
                f"ALTER TABLE {q(tname)} DROP COLUMN {q(cname)};"],
            down=[f"-- cannot auto-restore dropped column {tname}.{cname}; restore from backup."],
            reversible=False, requires_backup=True,
        )
    if name == "rename_column":
        tname = table.name if table else table_id
        up, down = dialect.rename_column_sql(tname, op.get("from"), op.get("to"))
        return SqlStep(op=name, target=f"{tname}.{op.get('to')}", up=up, down=down)
    if name == "rename_table":
        up, down = dialect.rename_table_sql(op.get("from"), op.get("to"))
        return SqlStep(op=name, target=op.get("to"), up=up, down=down)
    if name == "change_type":
        return _emit_change_type(table, field_id, op, dialect, reg)
    if name == "set_not_null":
        cname = _col_name(table, field_id or "")
        tname = table.name if table else table_id
        type_str = _field_type(table, field_id, dialect, reg)
        up, down, note = dialect.set_not_null_sql(tname, cname, type_str,
                                                  recommended=hint.get("recommended_clause") or None)
        return SqlStep(op=name, target=f"{tname}.{cname}", up=up, down=down, note=note)
    if name == "drop_not_null":
        cname = _col_name(table, field_id or "")
        tname = table.name if table else table_id
        up, down = dialect.drop_not_null_sql(tname, cname, _field_type(table, field_id, dialect, reg))
        return SqlStep(op=name, target=f"{tname}.{cname}", up=up, down=down)
    if name == "change_default":
        cname = _col_name(table, field_id or "")
        tname = table.name if table else table_id
        new = op.get("to")
        old = op.get("from")
        up, down = dialect.change_default_sql(
            tname, cname,
            _render_default(new) if new is not None else None,
            _render_default(old) if old is not None else None,
        )
        return SqlStep(op=name, target=f"{tname}.{cname}", up=up, down=down)
    if name == "set_primary_key":
        cname = _col_name(table, field_id or "")
        tname = table.name if table else table_id
        return SqlStep(op=name, target=f"{tname}.{cname}",
                       up=[f"ALTER TABLE {q(tname)} ADD PRIMARY KEY ({q(cname)});"],
                       down=[dialect.drop_primary_key_sql(tname)])
    if name == "add_index":
        return _emit_add_index(table, entity_id, op, dialect)
    if name == "drop_index":
        return _emit_drop_index(table, op, dialect)
    if name == "add_relation":
        return _emit_add_relation(schema, entity_id, dialect, reg)
    if name == "drop_relation":
        return _emit_drop_relation(schema, entity_id, dialect)
    return None  # pragma: no cover - any other op is treated as non-DDL


def _field_type(table: Table | None, field_id: str | None, dialect: SqlDialect, reg: TypeRegistry) -> str:
    field = table.field_by_id(field_id) if (table and field_id) else None
    return dialect.render_type(field, reg) if field else ""


def _emit_create_table(table: Table | None, dialect: SqlDialect, reg: TypeRegistry,
                       fk_overrides: dict[str, str] | None = None) -> SqlStep | None:
    if table is None:
        return None
    fk_overrides = fk_overrides or {}
    pks = table.primary_keys()
    lines = [f"  {_column_def(f, dialect, reg, type_override=fk_overrides.get(f.id))}" for f in table.fields]
    if len(pks) >= 1:
        cols = ", ".join(dialect.q(f.name) for f in pks)
        lines.append(f"  PRIMARY KEY ({cols})")
    body = ",\n".join(lines)
    return SqlStep(op="add_table", target=table.name, up=[dialect.create_table_sql(table.name, body)],
                   down=[dialect.drop_table_sql(table.name)])


def _emit_add_column(table: Table | None, field_id: str | None, op: dict[str, Any],
                     dialect: SqlDialect, reg: TypeRegistry, fk_overrides: dict[str, str] | None = None) -> SqlStep:
    fk_overrides = fk_overrides or {}
    tname = table.name if table else op.get("tableId")
    field = table.field_by_id(field_id) if (table and field_id) else None
    if field is not None:
        col = _column_def(field, dialect, reg, type_override=fk_overrides.get(field.id))
        cname = field.name
    else:  # fall back to the inline op payload when the field isn't in the resolved schema
        payload = op.get("field") or {}
        cname = payload.get("name", field_id)
        col = f"{dialect.q(cname)} text" + ("" if payload.get("nullable", True) else " NOT NULL")
    return SqlStep(op="add_column", target=f"{tname}.{cname}",
                   up=[f"ALTER TABLE {dialect.q(tname)} {dialect.add_column_clause} {col};"],
                   down=[f"ALTER TABLE {dialect.q(tname)} DROP COLUMN {dialect.q(cname)};"])


def _emit_change_type(table: Table | None, field_id: str | None, op: dict[str, Any],
                      dialect: SqlDialect, reg: TypeRegistry) -> SqlStep:
    tname = table.name if table else op.get("tableId")
    field = table.field_by_id(field_id) if (table and field_id) else None
    cname = field.name if field else field_id
    new_type = dialect.render_type(field, reg) if field else str(op.get("to") or "text")
    old_type = str(op.get("from") or "text")
    up, down = dialect.change_type_sql(tname, cname, new_type, old_type)
    return SqlStep(op="change_type", target=f"{tname}.{cname}", up=up, down=down)


def _emit_add_index(table: Table | None, entity_id: str | None, op: dict[str, Any], dialect: SqlDialect) -> SqlStep:
    tname = table.name if table else op.get("tableId")
    columns = op.get("columns") or []
    unique = bool(op.get("unique"))
    col_names = [_col_name(table, c) for c in columns]
    iname = _index_name(table, columns, unique=unique) if table else (entity_id or "idx")
    up, down = dialect.add_index_sql(iname, tname, col_names, unique=unique)
    return SqlStep(op="add_index", target=iname, up=up, down=down)


def _emit_drop_index(table: Table | None, op: dict[str, Any], dialect: SqlDialect) -> SqlStep:
    tname = table.name if table else op.get("tableId")
    columns = op.get("columns") or []
    unique = bool(op.get("unique"))
    iname = _index_name(table, columns, unique=unique) if table else (op.get("entityId") or "idx")
    up, down = dialect.drop_index_sql(iname, tname)
    return SqlStep(op="drop_index", target=iname, up=up, down=down)


def _emit_add_relation(schema: SchemaJson, entity_id: str | None, dialect: SqlDialect,
                       reg: TypeRegistry) -> SqlStep | None:
    rel = next((r for r in schema.logical.relations if r.id == entity_id), None)
    if rel is None or not rel.from_table_id:
        return None
    from_table = schema.table_by_id(rel.from_table_id)
    to_table = schema.table_by_id(rel.to_table_id) if rel.to_table_id else None
    if from_table is None or to_table is None:
        return None
    q = dialect.q
    fk_field = from_table.field_by_id(rel.foreign_key_field_id) if rel.foreign_key_field_id else None
    fk_col = fk_field.name if fk_field else f"{to_table.name}_id"
    ref_col = _pk_column(to_table)
    cname = f"{from_table.name}_{fk_col}_fkey"
    clauses = ""
    if rel.on_delete:
        clauses += f" ON DELETE {rel.on_delete.upper()}"
    if rel.on_update:
        clauses += f" ON UPDATE {rel.on_update.upper()}"
    up = (f"ALTER TABLE {q(from_table.name)} ADD CONSTRAINT {q(cname)} "
          f"FOREIGN KEY ({q(fk_col)}) REFERENCES {q(to_table.name)} ({q(ref_col)}){clauses};")
    return SqlStep(op="add_relation", target=cname, up=[up],
                   down=[f"ALTER TABLE {q(from_table.name)} DROP CONSTRAINT {q(cname)};"])


def _emit_drop_relation(schema: SchemaJson, entity_id: str | None, dialect: SqlDialect) -> SqlStep | None:
    rel = next((r for r in schema.logical.relations if r.id == entity_id), None)
    q = dialect.q
    # The relation no longer exists in the target schema for a real drop; emit a best-effort name.
    if rel is not None and rel.from_table_id:
        from_table = schema.table_by_id(rel.from_table_id)
        if from_table is not None:
            fk_field = from_table.field_by_id(rel.foreign_key_field_id) if rel.foreign_key_field_id else None
            fk_col = fk_field.name if fk_field else "fk"
            cname = f"{from_table.name}_{fk_col}_fkey"
            return SqlStep(op="drop_relation", target=cname,
                           up=[f"ALTER TABLE {q(from_table.name)} DROP CONSTRAINT {q(cname)};"],
                           down=[f"-- re-add foreign key {cname} manually."])
    return SqlStep(op="drop_relation", target=entity_id,
                   up=[f"-- drop foreign key {entity_id} (constraint name not resolvable)."],
                   down=[])


# --------------------------------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------------------------------
def emit_sql(operations: list[dict[str, Any]], schema: SchemaJson, *,
             driver: str = "postgres", registry: TypeRegistry | None = None) -> SqlScript:
    """Render a Diff operation list into an ordered :class:`SqlScript` for ``driver``.

    ``schema`` is the *target* (approved) schema, used to resolve ids → names and semantic →
    physical types. Operations are assumed to already be in safe execution order (the Diff Engine
    topologically sorts them). Non-DDL operations are skipped and listed in ``script.skipped``.
    """
    if driver not in SUPPORTED_DRIVERS:
        raise ValueError(f"SQL emitter supports {SUPPORTED_DRIVERS}, not {driver!r}")
    reg = registry or DEFAULT_REGISTRY
    dialect = get_dialect(driver)
    fk_overrides = _fk_type_overrides(schema, dialect, reg)
    steps: list[SqlStep] = []
    skipped: list[str] = []
    for op in operations:
        step = _emit_op(op, schema, dialect, reg, fk_overrides)
        if step is None:
            skipped.append(op["op"])
        else:
            steps.append(step)
    return SqlScript(driver=dialect.name, steps=steps, skipped=skipped)
