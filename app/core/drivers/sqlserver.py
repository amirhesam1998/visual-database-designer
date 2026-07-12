"""SQL Server / T-SQL driver — the third implementation of the driver pattern (multi-driver §0).

This module is the milestone's *architecture test*: adding SQL Server should be a single new module
plus one line in the registry, with the Core (``schema_json``, diff, risk, the resolve pipeline)
untouched. The three driver-aware concerns live here and nowhere else:

* a :class:`SqlDialect` for **emit** — T-SQL syntax kept as data/behaviour: ``[bracket]`` quoting,
  ``IDENTITY(1,1)`` rendered into the type (the way Postgres renders ``serial``, so the column
  ordering is valid T-SQL), and the ALTER shapes that genuinely differ (``sp_rename``, named default
  constraints);
* **introspection** over the MSSQL ``sys`` catalog (distinct from the ``information_schema`` shape
  Postgres/MySQL use — identity, ``uniqueidentifier`` and the real referential actions all come from
  ``sys.*``);
* the **reverse type map** — native MSSQL column → physical spec + semantic hint.

The forward map (semantic → physical, e.g. ``uuid`` → ``UNIQUEIDENTIFIER``, ``boolean`` → ``BIT``,
``datetime`` → ``DATETIME2`` because T-SQL ``TIMESTAMP`` is a rowversion, not a clock) lives in the
Type System, shared with the emitter, so the whole-project FK lesson holds for free: a uuid primary
key is ``UNIQUEIDENTIFIER`` and its foreign-key column inherits exactly that (spec §1).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.drivers.base import (
    IntrospectedColumn,
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
# Dialect (emit) — T-SQL syntax.
# ==================================================================================================
class SqlServerDialect(SqlDialect):
    name = "sqlserver"
    quote_char = "["          # cosmetic; ``q`` is overridden because T-SQL quotes with a [ ] *pair*
    table_options = ""
    drop_table_clause = ""    # SQL Server has no DROP TABLE … CASCADE; reset() drops FKs first instead
    autoincrement_keyword = ""  # IDENTITY is rendered into the type (see render_type), not appended
    add_column_clause = "ADD"   # T-SQL: ALTER TABLE … ADD <coldef> (no COLUMN keyword)

    # T-SQL stores Unicode in the national types; map the portable names so Persian/CJK text is safe by
    # default (NVARCHAR over VARCHAR, NCHAR over CHAR). NVARCHAR(MAX) already arrives as its own type.
    _TYPE_ALIASES = {"varchar": "nvarchar", "char": "nchar"}

    def q(self, identifier: str) -> str:
        """Quote a T-SQL identifier with ``[ ]``, doubling any embedded ``]``."""
        return "[" + str(identifier).replace("]", "]]") + "]"

    def physical_to_type(self, p: dict[str, Any]) -> str:
        base = str(p.get("type", "text"))
        aliased = self._TYPE_ALIASES.get(base)
        return render_physical({**p, "type": aliased} if aliased else p)

    def render_type(self, field: Field_, reg: TypeRegistry) -> str:
        """Render the physical type, folding ``IDENTITY(1,1)`` into the type for an auto-increment key.

        T-SQL grammar wants the IDENTITY property *before* the NULL/NOT NULL constraint, so — like
        Postgres' ``serial`` — it belongs in the type string, not as a trailing column attribute."""
        p = self.physical(field, reg)
        base = str(p.get("type", "text"))
        if field.auto_increment and base in {"int", "integer", "bigint", "smallint"}:
            return f"{self.physical_to_type(p)} IDENTITY(1,1)"
        return self.physical_to_type(p)

    def add_index_sql(self, iname: str, tname: str, cols: list[str], *, unique: bool) -> tuple[list[str], list[str]]:
        unique_kw = "UNIQUE " if unique else ""
        cols_sql = ", ".join(self.q(c) for c in cols)
        up = [f"CREATE {unique_kw}INDEX {self.q(iname)} ON {self.q(tname)} ({cols_sql});"]
        down = [f"DROP INDEX {self.q(iname)} ON {self.q(tname)};"]
        return up, down

    def drop_index_sql(self, iname: str, tname: str) -> tuple[list[str], list[str]]:
        return ([f"DROP INDEX {self.q(iname)} ON {self.q(tname)};"],
                [f"-- recreate index {iname} manually (definition not retained in the drop op)."])

    def change_type_sql(self, tname: str, cname: str, new_type: str, old_type: str) -> tuple[list[str], list[str]]:
        return ([f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {new_type};"],
                [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {old_type};"])

    def set_not_null_sql(self, tname: str, cname: str, type_str: str, *, recommended: str | None = None
                         ) -> tuple[list[str], list[str], str | None]:
        # T-SQL toggles nullability via ALTER COLUMN, which must restate the column type (as MySQL does).
        ts = type_str or "nvarchar(255)"
        up = [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {ts} NOT NULL;"]
        down = [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {ts} NULL;"]
        return up, down, recommended

    def drop_not_null_sql(self, tname: str, cname: str, type_str: str) -> tuple[list[str], list[str]]:
        ts = type_str or "nvarchar(255)"
        return ([f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {ts} NULL;"],
                [f"ALTER TABLE {self.q(tname)} ALTER COLUMN {self.q(cname)} {ts} NOT NULL;"])

    def drop_primary_key_sql(self, tname: str) -> str:
        # SQL Server PK constraint names are server-generated unless explicitly named; this drops by the
        # convention name an emitter-created PK would carry — adjust if the live name differs.
        return f"ALTER TABLE {self.q(tname)} DROP CONSTRAINT {self.q('PK_' + tname)};"

    def rename_column_sql(self, tname: str, frm: str, to: str) -> tuple[list[str], list[str]]:
        # T-SQL renames through sp_rename; the *new* name is passed unqualified and unquoted.
        return ([f"EXEC sp_rename '{tname}.{frm}', '{to}', 'COLUMN';"],
                [f"EXEC sp_rename '{tname}.{to}', '{frm}', 'COLUMN';"])

    def rename_table_sql(self, frm: str, to: str) -> tuple[list[str], list[str]]:
        return ([f"EXEC sp_rename '{frm}', '{to}';"],
                [f"EXEC sp_rename '{to}', '{frm}';"])

    def change_default_sql(self, tname: str, cname: str, new_literal: str | None, old_literal: str | None
                           ) -> tuple[list[str], list[str]]:
        """T-SQL has no ``ALTER COLUMN … SET DEFAULT``; defaults are named constraints added/dropped
        separately. We name them deterministically (``DF_<table>_<col>``) so the rollback can drop them."""
        dn = self.q(f"DF_{tname}_{cname}")
        t, c = self.q(tname), self.q(cname)

        def steps(add_lit: str | None, drop_existing: bool) -> list[str]:
            out: list[str] = []
            if drop_existing:
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT {dn};")
            if add_lit is not None:
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {dn} DEFAULT {add_lit} FOR {c};")
            return out

        return steps(new_literal, old_literal is not None), steps(old_literal, new_literal is not None)


# ==================================================================================================
# Connection (impure) — pymssql preferred, pyodbc fallback. Lazy so the module loads without either.
# ==================================================================================================
def _dsn_parts(dsn: str) -> dict[str, Any]:
    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 1433,
        "user": unquote(parsed.username) if parsed.username else "sa",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": (parsed.path or "/").lstrip("/") or None,
    }


def connect(dsn: str):  # noqa: ANN201 - returns a DB-API connection
    parts = _dsn_parts(dsn)
    try:
        import pymssql  # type: ignore

        conn = pymssql.connect(
            server=parts["host"], port=str(parts["port"]), user=parts["user"],
            password=parts["password"], database=parts["database"] or "",
        )
        conn.autocommit(True)
        return conn
    except ModuleNotFoundError:
        try:
            import pyodbc  # type: ignore

            cs = (
                "DRIVER={ODBC Driver 18 for SQL Server};"
                f"SERVER={parts['host']},{parts['port']};DATABASE={parts['database'] or ''};"
                f"UID={parts['user']};PWD={parts['password']};TrustServerCertificate=yes"
            )
            return pyodbc.connect(cs, autocommit=True)
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "no SQL Server driver installed; add pymssql (or pyodbc) to import a SQL Server database"
            ) from exc


def _lit(value: str) -> str:
    """A single-quoted SQL string literal (the schema name is inlined since pymssql and pyodbc use
    different parameter placeholders — ``%s`` vs ``?`` — and the value is a controlled identifier)."""
    return "'" + str(value).replace("'", "''") + "'"


# ==================================================================================================
# Introspection (impure) — MSSQL sys catalog (distinct from Postgres/MySQL information_schema; §3).
# ==================================================================================================
def _q_tables(s: str) -> str:
    return ("SELECT t.name FROM sys.tables t JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
            f"WHERE sc.name = {_lit(s)} ORDER BY t.name")


def _q_columns(s: str) -> str:
    return ("SELECT t.name AS table_name, c.name AS column_name, c.column_id, ty.name AS data_type, "
            "c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, "
            "OBJECT_DEFINITION(c.default_object_id) AS column_default "
            "FROM sys.columns c "
            "JOIN sys.tables t ON t.object_id = c.object_id "
            "JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
            "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
            f"WHERE sc.name = {_lit(s)} ORDER BY t.name, c.column_id")


def _q_primary_keys(s: str) -> str:
    return ("SELECT t.name AS table_name, col.name AS column_name, ic.key_ordinal "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
            f"WHERE i.is_primary_key = 1 AND sc.name = {_lit(s)} ORDER BY t.name, ic.key_ordinal")


def _q_foreign_keys(s: str) -> str:
    return ("SELECT fk.name AS constraint_name, tp.name AS table_name, cp.name AS column_name, "
            "fkc.constraint_column_id, tr.name AS ref_table, cr.name AS ref_column, "
            "fk.delete_referential_action_desc, fk.update_referential_action_desc "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
            "JOIN sys.schemas sp ON sp.schema_id = tp.schema_id "
            "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
            "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
            "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id "
            "AND cr.column_id = fkc.referenced_column_id "
            f"WHERE sp.name = {_lit(s)} ORDER BY fk.name, fkc.constraint_column_id")


def _q_indexes(s: str) -> str:
    return ("SELECT t.name AS table_name, i.name AS index_name, i.is_unique, "
            "col.name AS column_name, ic.key_ordinal "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
            f"WHERE i.is_primary_key = 0 AND i.type > 0 AND i.name IS NOT NULL AND ic.is_included_column = 0 "
            f"AND sc.name = {_lit(s)} ORDER BY t.name, i.name, ic.key_ordinal")


def _rule(rule: str | None) -> str | None:
    if not rule:
        return None
    norm = rule.strip().lower().replace(" ", "_")
    return None if norm in {"no_action", "restrict"} else norm  # NO_ACTION is SQL Server's default


def _char_length(data_type: str, max_length: int | None) -> int | None:
    """Convert sys.columns.max_length (bytes) to a character length; -1 means MAX, n* types are 2 bytes."""
    if max_length is None:
        return None
    if max_length == -1:
        return -1
    if data_type in {"nchar", "nvarchar"}:
        return max_length // 2
    return max_length


def introspect(dsn: str, *, schema: str | None = None) -> IntrospectedSchema:
    """Read a live SQL Server schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    sch = schema or "dbo"
    conn = connect(dsn)
    try:
        cur = conn.cursor()

        def rows(sql: str) -> list[tuple]:
            cur.execute(sql)
            return list(cur.fetchall())

        tables: dict[str, IntrospectedTable] = {r[0]: IntrospectedTable(name=r[0]) for r in rows(_q_tables(sch))}
        for (tname, cname, _cid, dtype, mlen, prec, scale, is_null, is_ident, default) in rows(_q_columns(sch)):
            if tname not in tables:
                continue
            dt = str(dtype).lower()
            tables[tname].columns.append(IntrospectedColumn(
                name=cname, data_type=dt,
                nullable=bool(is_null), default=default,
                char_max_length=_char_length(dt, mlen),
                numeric_precision=prec if dt in {"decimal", "numeric"} else None,
                numeric_scale=scale if dt in {"decimal", "numeric"} else None,
                is_identity=bool(is_ident),
            ))

        for (tname, cname, _ord) in rows(_q_primary_keys(sch)):
            if tname in tables:
                tables[tname].primary_key.append(cname)

        # Key by (table, constraint), consistent with the other drivers and the index accumulation
        # below — never by constraint name alone, which would merge two same-named FKs on different
        # tables into one and drop a relation.
        fks: dict[tuple[str, str], IntrospectedForeignKey] = {}
        for (cstr, tname, col, _ord, ref_table, ref_col, del_rule, upd_rule) in rows(_q_foreign_keys(sch)):
            fk = fks.get((tname, cstr))
            if fk is None:
                fk = IntrospectedForeignKey(name=cstr, table=tname, ref_table=ref_table,
                                            on_delete=_rule(del_rule), on_update=_rule(upd_rule))
                fks[(tname, cstr)] = fk
            fk.columns.append(col)
            fk.ref_columns.append(ref_col)

        idx: dict[tuple[str, str], IntrospectedIndex] = {}
        for (tname, iname, is_unique, col, _seq) in rows(_q_indexes(sch)):
            key = (tname, iname)
            if key not in idx:
                idx[key] = IntrospectedIndex(name=iname, table=tname, unique=bool(is_unique))
            idx[key].columns.append(col)

        return IntrospectedSchema(
            tables=sorted(tables.values(), key=lambda t: t.name),
            foreign_keys=sorted(fks.values(), key=lambda f: f.name),
            indexes=sorted(idx.values(), key=lambda i: (i.table, i.name)),
            enums=[],  # SQL Server has no native enum type (string-backed CHECKs are out of scope)
        )
    finally:
        conn.close()


def apply_sql(dsn: str, statements: list[str]) -> None:
    """Apply raw-SQL statements in order to a (shadow) SQL Server database (impure)."""
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        for stmt in statements:
            s = stmt.strip()
            if s and not s.startswith("--") and s.upper() != "GO":  # GO is a client batch separator, not T-SQL
                cur.execute(s)
    finally:
        conn.close()


def reset(dsn: str, *, schema: str | None = None) -> None:
    """Reset a shadow SQL Server schema by dropping every foreign key, then every table (FK first so the
    drop order doesn't matter — SQL Server has no global constraint-checks toggle)."""
    sch = schema or "dbo"
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 'ALTER TABLE [' + sc.name + '].[' + t.name + '] DROP CONSTRAINT [' + fk.name + ']' "
            "FROM sys.foreign_keys fk "
            "JOIN sys.tables t ON t.object_id = fk.parent_object_id "
            f"JOIN sys.schemas sc ON sc.schema_id = t.schema_id WHERE sc.name = {_lit(sch)}"
        )
        for (drop_fk,) in list(cur.fetchall()):
            cur.execute(drop_fk)
        cur.execute(_q_tables(sch))
        for (tname,) in list(cur.fetchall()):
            cur.execute(f"DROP TABLE IF EXISTS [{sch}].[{tname}]")
    finally:
        conn.close()


# ==================================================================================================
# Reverse type map (native MSSQL column → physical spec + semantic hint) — spec §1/§3.
# ==================================================================================================
def column_physical(col: IntrospectedColumn) -> dict[str, Any]:
    dt = (col.data_type or "").strip().lower()
    clen = col.char_max_length
    if dt in {"nvarchar", "varchar"}:
        if clen == -1:  # NVARCHAR(MAX)/VARCHAR(MAX) → free text (the json/text mapping; spec §1)
            return {"type": "text"}
        return {"type": "varchar", **({"length": clen} if clen else {})}
    if dt in {"nchar", "char"}:
        return {"type": "char", **({"length": clen} if clen else {})}
    if dt in {"text", "ntext"}:
        return {"type": "text"}
    if dt == "uniqueidentifier":
        return {"type": "uniqueidentifier"}  # native uuid → inferred back to uuid
    if dt == "bit":
        return {"type": "bit"}
    if dt == "tinyint":
        return {"type": "smallint"}  # MSSQL TINYINT is 0–255; the nearest portable key type is smallint
    if dt == "smallint":
        return {"type": "smallint"}
    if dt == "int":
        return {"type": "integer"}
    if dt == "bigint":
        return {"type": "bigint"}
    if dt in {"decimal", "numeric"}:
        spec: dict[str, Any] = {"type": "decimal"}
        if col.numeric_precision is not None:
            spec["precision"] = col.numeric_precision
            spec["scale"] = col.numeric_scale or 0
        return spec
    if dt in {"money", "smallmoney"}:
        return {"type": "decimal", "precision": 19, "scale": 4}
    if dt in {"float", "real"}:
        return {"type": "float"}
    if dt in {"datetime2", "datetime", "smalldatetime", "datetimeoffset"}:
        return {"type": "datetime2"}
    if dt == "date":
        return {"type": "date"}
    if dt == "time":
        return {"type": "time"}
    return {"type": dt or "text"}


def is_autoincrement(col: IntrospectedColumn) -> bool:
    return col.is_identity  # set from sys.columns.is_identity during introspection


def semantic_override(col: IntrospectedColumn, physical: dict[str, Any]) -> str | None:
    """Driver-specific reverse rules: BIT is the canonical boolean, DATETIME2 the canonical datetime.
    (``uniqueidentifier`` → uuid is handled by the shared inference, which already knows that name.)"""
    t = physical.get("type")
    if t == "bit":
        return "boolean"
    if t == "datetime2":
        return "datetime"
    return None
