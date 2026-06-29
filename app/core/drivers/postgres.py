"""PostgreSQL driver — the first implementation of the driver pattern (multi-driver milestone §0).

This is a *refactor*, not a rewrite: the introspection queries, the reverse type map and the DDL
dialect are exactly what the importer/emitter emitted before — relocated behind the :class:`Driver`
contract so a second database (MySQL) is a sibling module, not a fork of the Core. Postgres snapshots
stay byte-for-byte identical (the milestone's zero-regression guarantee, spec §5).
"""

from __future__ import annotations

from typing import Any

from app.core.drivers.base import (
    IntrospectedColumn,
    IntrospectedEnum,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedSchema,
    IntrospectedTable,
    SqlDialect,
    render_physical,
)
from app.core.schema_json import Field_
from app.core.type_system import TypeRegistry


# ==================================================================================================
# Dialect (emit) — reproduces the Milestone-1 Postgres DDL exactly.
# ==================================================================================================
class PostgresDialect(SqlDialect):
    name = "postgres"
    quote_char = '"'
    table_options = ""
    drop_table_clause = " CASCADE"
    autoincrement_keyword = ""  # Postgres uses serial/bigserial *types*, not a column attribute

    def render_type(self, field: Field_, reg: TypeRegistry) -> str:
        p = self.physical(field, reg)
        base = str(p.get("type", "text"))
        # Auto-increment integers become Postgres serial/bigserial so the column self-populates.
        if field.auto_increment and base in {"integer", "smallint", "bigint"}:
            return {"bigint": "bigserial", "smallint": "smallserial"}.get(base, "serial")
        return render_physical(p)

    def add_index_sql(self, iname: str, tname: str, cols: list[str], *, unique: bool) -> tuple[list[str], list[str]]:
        unique_kw = "UNIQUE " if unique else ""
        cols_sql = ", ".join(self.q(c) for c in cols)
        up = ["-- run outside a transaction (CONCURRENTLY cannot run inside one)",
              f"CREATE {unique_kw}INDEX CONCURRENTLY IF NOT EXISTS {self.q(iname)} ON {self.q(tname)} ({cols_sql});"]
        down = [f"DROP INDEX CONCURRENTLY IF EXISTS {self.q(iname)};"]
        return up, down

    def drop_index_sql(self, iname: str, tname: str) -> tuple[list[str], list[str]]:
        return ([f"DROP INDEX CONCURRENTLY IF EXISTS {self.q(iname)};"],
                [f"-- recreate index {iname} manually (definition not retained in the drop op)."])

    def change_type_sql(self, tname: str, cname: str, new_type: str, old_type: str) -> tuple[list[str], list[str]]:
        up = [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} TYPE {new_type} "
              f"USING {self.q(cname)}::{new_type};"]
        down = [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} TYPE {old_type} "
                f"USING {self.q(cname)}::{old_type};"]
        return up, down

    def set_not_null_sql(self, tname: str, cname: str, type_str: str, *, recommended: str | None = None
                         ) -> tuple[list[str], list[str], str | None]:
        up: list[str] = []
        if recommended:
            up.append(f"-- safer on large tables: {recommended}")
        up.append(f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} SET NOT NULL;")
        down = [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} DROP NOT NULL;"]
        return up, down, recommended

    def drop_not_null_sql(self, tname: str, cname: str, type_str: str) -> tuple[list[str], list[str]]:
        return ([f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} DROP NOT NULL;"],
                [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} SET NOT NULL;"])

    def drop_primary_key_sql(self, tname: str) -> str:
        return f"ALTER TABLE {self.q(tname)} DROP CONSTRAINT {self.q(tname + '_pkey')};"


# ==================================================================================================
# Introspection (impure — the only place that touches a database).
# ==================================================================================================
_Q_TABLES = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = %s AND table_type = 'BASE TABLE'
ORDER BY table_name;
"""

_Q_COLUMNS = """
SELECT table_name, column_name, ordinal_position, data_type, udt_name,
       is_nullable, column_default, character_maximum_length,
       numeric_precision, numeric_scale, is_identity
FROM information_schema.columns
WHERE table_schema = %s
ORDER BY table_name, ordinal_position;
"""

_Q_PRIMARY_KEYS = """
SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
ORDER BY tc.table_name, kcu.ordinal_position;
"""

_Q_FOREIGN_KEYS = """
SELECT tc.constraint_name, tc.table_name, kcu.column_name, kcu.ordinal_position,
       ccu.table_name AS ref_table, ccu.column_name AS ref_column,
       rc.delete_rule, rc.update_rule
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
JOIN information_schema.constraint_column_usage ccu
  ON rc.unique_constraint_name = ccu.constraint_name AND rc.unique_constraint_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
ORDER BY tc.constraint_name, kcu.ordinal_position;
"""

_Q_INDEXES = """
SELECT t.relname AS table_name, i.relname AS index_name,
       ix.indisunique AS is_unique, ix.indisprimary AS is_primary,
       a.attname AS column_name, k.ord
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND t.relkind = 'r'
ORDER BY t.relname, i.relname, k.ord;
"""

_Q_ENUMS = """
SELECT t.typname AS enum_name, e.enumlabel AS label, e.enumsortorder
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = %s
ORDER BY t.typname, e.enumsortorder;
"""

_Q_CHECKS = """
SELECT tc.table_name, tc.constraint_name, cc.check_clause
FROM information_schema.table_constraints tc
JOIN information_schema.check_constraints cc
  ON tc.constraint_name = cc.constraint_name AND tc.table_schema = cc.constraint_schema
WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = %s
ORDER BY tc.table_name, tc.constraint_name;
"""


def connect(dsn: str):  # noqa: ANN201 - returns a psycopg/psycopg2 connection
    """Lazily import a Postgres driver (psycopg 3 preferred, psycopg2 fallback) and connect."""
    try:
        import psycopg  # type: ignore

        return psycopg.connect(dsn)
    except ModuleNotFoundError:
        try:
            import psycopg2  # type: ignore

            return psycopg2.connect(dsn)
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "no Postgres driver installed; add psycopg[binary] (or psycopg2) to import a database"
            ) from exc


def _rule(rule: str | None) -> str | None:
    """Normalise a referential action; drop the Postgres default (NO ACTION) to reduce noise."""
    if not rule:
        return None
    norm = rule.strip().lower().replace(" ", "_")
    return None if norm == "no_action" else norm


def introspect(dsn: str, *, schema: str = "public") -> IntrospectedSchema:
    """Read a live Postgres schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            def rows(sql: str) -> list[tuple]:
                cur.execute(sql, (schema,))
                return list(cur.fetchall())

            tables: dict[str, IntrospectedTable] = {
                r[0]: IntrospectedTable(name=r[0]) for r in rows(_Q_TABLES)
            }
            for (tname, cname, _ord, dtype, udt, is_null, default, clen, nprec, nscale, ident) in rows(_Q_COLUMNS):
                if tname not in tables:
                    continue
                tables[tname].columns.append(IntrospectedColumn(
                    name=cname, data_type=dtype, udt_name=udt,
                    nullable=(str(is_null).upper() == "YES"),
                    default=default, char_max_length=clen,
                    numeric_precision=nprec, numeric_scale=nscale,
                    is_identity=(str(ident).upper() == "YES"),
                ))
            for (tname, cname, _ord) in rows(_Q_PRIMARY_KEYS):
                if tname in tables:
                    tables[tname].primary_key.append(cname)
            for (tname, cstr, clause) in rows(_Q_CHECKS):
                # Skip the implicit NOT NULL checks Postgres reports here — they are column attributes.
                if tname in tables and "IS NOT NULL" not in (clause or "").upper():
                    tables[tname].checks.append({"name": cstr, "clause": clause})

            fks: dict[str, IntrospectedForeignKey] = {}
            for (cstr, tname, col, _ord, ref_table, ref_col, del_rule, upd_rule) in rows(_Q_FOREIGN_KEYS):
                fk = fks.get(cstr)
                if fk is None:
                    fk = IntrospectedForeignKey(name=cstr, table=tname, ref_table=ref_table,
                                                on_delete=_rule(del_rule), on_update=_rule(upd_rule))
                    fks[cstr] = fk
                fk.columns.append(col)
                fk.ref_columns.append(ref_col)

            idx: dict[tuple[str, str], IntrospectedIndex] = {}
            for (tname, iname, is_unique, is_primary, col, _ord) in rows(_Q_INDEXES):
                if is_primary:
                    continue  # the PK index is represented by the table's primary_key, not as an index
                key = (tname, iname)
                if key not in idx:
                    idx[key] = IntrospectedIndex(name=iname, table=tname, unique=bool(is_unique))
                idx[key].columns.append(col)

            enums: dict[str, IntrospectedEnum] = {}
            for (ename, label, _ord) in rows(_Q_ENUMS):
                enums.setdefault(ename, IntrospectedEnum(name=ename)).labels.append(label)

        return IntrospectedSchema(
            tables=sorted(tables.values(), key=lambda t: t.name),
            foreign_keys=sorted(fks.values(), key=lambda f: f.name),
            indexes=sorted(idx.values(), key=lambda i: (i.table, i.name)),
            enums=sorted(enums.values(), key=lambda e: e.name),
        )
    finally:
        conn.close()


def apply_sql(dsn: str, statements: list[str]) -> None:
    """Apply raw-SQL statements in order to a (shadow) Postgres database (impure).

    ``autocommit`` is on so statements that cannot run in a transaction (e.g.
    ``CREATE INDEX CONCURRENTLY``) work, mirroring how the emitter's migrations are meant to run.
    """
    conn = connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                s = stmt.strip()
                if s and not s.startswith("--"):
                    cur.execute(s)
    finally:
        conn.close()


def reset(dsn: str, *, schema: str = "public") -> None:
    """Reset a shadow schema so an import reflects only the supplied dump (``DROP SCHEMA … CASCADE``)."""
    apply_sql(dsn, [f'DROP SCHEMA IF EXISTS "{schema}" CASCADE', f'CREATE SCHEMA "{schema}"'])


# ==================================================================================================
# Reverse type map (native column → physical spec) — unchanged from the original importer.
# ==================================================================================================
def column_physical(col: IntrospectedColumn) -> dict[str, Any]:
    """Map an introspected Postgres column to a physical spec dict (``{type, length?, …}``)."""
    dt = (col.data_type or "").strip().lower()
    udt = (col.udt_name or "").strip().lower()
    if dt in {"character varying", "varchar"}:
        return {"type": "varchar", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt in {"character", "char", "bpchar"}:
        return {"type": "char", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt == "text":
        return {"type": "text"}
    if dt in {"integer", "int", "int4"}:
        return {"type": "integer"}
    if dt in {"bigint", "int8"}:
        return {"type": "bigint"}
    if dt in {"smallint", "int2"}:
        return {"type": "smallint"}
    if dt in {"numeric", "decimal"}:
        spec: dict[str, Any] = {"type": "numeric"}
        if col.numeric_precision is not None:
            spec["precision"] = col.numeric_precision
            spec["scale"] = col.numeric_scale or 0
        return spec
    if dt in {"double precision", "real"}:
        return {"type": dt}
    if dt == "boolean":
        return {"type": "boolean"}
    if dt == "uuid":
        return {"type": "uuid"}
    if dt in {"timestamp without time zone", "timestamp with time zone", "timestamp"}:
        return {"type": "timestamp"}
    if dt == "date":
        return {"type": "date"}
    if dt in {"time without time zone", "time with time zone", "time"}:
        return {"type": "time"}
    if dt == "jsonb":
        return {"type": "jsonb"}
    if dt == "json":
        return {"type": "json"}
    if dt == "user-defined" and udt:
        return {"type": udt}  # an enum or other user type; recorded as-is
    return {"type": udt or dt or "text"}


def is_autoincrement(col: IntrospectedColumn) -> bool:
    if col.is_identity:
        return True
    default = (col.default or "").lower()
    return "nextval(" in default


def semantic_override(col: IntrospectedColumn, physical: dict[str, Any]) -> str | None:
    """Postgres needs no driver-specific reverse rule beyond the generic inference."""
    return None
