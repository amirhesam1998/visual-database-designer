"""MySQL / MariaDB driver — the second implementation of the driver pattern (multi-driver milestone).

Adding a database is now a single module: a :class:`SqlDialect` (MySQL DDL syntax — backticks,
``AUTO_INCREMENT``, ``ENGINE=InnoDB``), MySQL ``information_schema`` introspection, and the reverse
type map (native column → physical + semantic hint). The forward map (semantic → physical, e.g.
``uuid`` → ``CHAR(36)``) lives in the Type System, shared with the emitter, so the FK lesson holds:
a uuid primary key is ``CHAR(36)`` and its foreign key inherits exactly that (spec §1).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.drivers.base import (
    IntrospectedColumn,
    IntrospectedEnum,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedSchema,
    IntrospectedTable,
    SqlDialect,
)


# ==================================================================================================
# Dialect (emit) — MySQL DDL syntax.
# ==================================================================================================
class MySqlDialect(SqlDialect):
    name = "mysql"
    quote_char = "`"
    table_options = " ENGINE=InnoDB"
    drop_table_clause = ""  # MySQL has no DROP TABLE … CASCADE; FK checks are toggled instead
    autoincrement_keyword = "AUTO_INCREMENT"

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
        return ([f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {new_type};"],
                [f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {old_type};"])

    def set_not_null_sql(self, tname: str, cname: str, type_str: str, *, recommended: str | None = None
                         ) -> tuple[list[str], list[str], str | None]:
        # MySQL nullability is set via MODIFY COLUMN, which must restate the column type.
        ts = type_str or "varchar(255)"
        up = [f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {ts} NOT NULL;"]
        down = [f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {ts} NULL;"]
        return up, down, recommended

    def drop_not_null_sql(self, tname: str, cname: str, type_str: str) -> tuple[list[str], list[str]]:
        ts = type_str or "varchar(255)"
        return ([f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {ts} NULL;"],
                [f"ALTER TABLE {self.q(tname)} MODIFY COLUMN {self.q(cname)} {ts} NOT NULL;"])

    def drop_primary_key_sql(self, tname: str) -> str:
        return f"ALTER TABLE {self.q(tname)} DROP PRIMARY KEY;"


# ==================================================================================================
# Connection (impure) — pymysql preferred, mysql-connector fallback. Lazy so the module loads without.
# ==================================================================================================
def _dsn_parts(dsn: str) -> dict[str, Any]:
    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else "root",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": (parsed.path or "/").lstrip("/") or None,
    }


def connect(dsn: str):  # noqa: ANN201 - returns a DB-API connection
    parts = _dsn_parts(dsn)
    try:
        import pymysql  # type: ignore

        return pymysql.connect(autocommit=True, **parts)
    except ModuleNotFoundError:
        try:
            import mysql.connector  # type: ignore

            return mysql.connector.connect(autocommit=True, **parts)
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "no MySQL driver installed; add PyMySQL (or mysql-connector-python) to import a MySQL database"
            ) from exc


def _db_name(dsn: str, schema: str | None) -> str:
    return schema or _dsn_parts(dsn)["database"] or ""


# ==================================================================================================
# Introspection (impure) — MySQL information_schema (distinct from Postgres; spec §2).
# ==================================================================================================
_Q_TABLES = ("SELECT TABLE_NAME FROM information_schema.TABLES "
             "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")

_Q_COLUMNS = ("SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, COLUMN_TYPE, "
              "IS_NULLABLE, COLUMN_DEFAULT, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
              "NUMERIC_SCALE, EXTRA FROM information_schema.COLUMNS "
              "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION")

_Q_PRIMARY_KEYS = ("SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION "
                   "FROM information_schema.KEY_COLUMN_USAGE "
                   "WHERE TABLE_SCHEMA = %s AND CONSTRAINT_NAME = 'PRIMARY' "
                   "ORDER BY TABLE_NAME, ORDINAL_POSITION")

_Q_FOREIGN_KEYS = ("SELECT k.CONSTRAINT_NAME, k.TABLE_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION, "
                   "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.DELETE_RULE, r.UPDATE_RULE "
                   "FROM information_schema.KEY_COLUMN_USAGE k "
                   "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
                   "  ON k.CONSTRAINT_NAME = r.CONSTRAINT_NAME AND k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA "
                   "WHERE k.TABLE_SCHEMA = %s AND k.REFERENCED_TABLE_NAME IS NOT NULL "
                   "ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION")

_Q_INDEXES = ("SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
              "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s "
              "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX")


def _rule(rule: str | None) -> str | None:
    if not rule:
        return None
    norm = rule.strip().lower().replace(" ", "_")
    return None if norm in {"no_action", "restrict"} else norm  # RESTRICT is MySQL's default


def _parse_enum(column_type: str) -> list[str]:
    """Extract labels from a MySQL ``enum('a','b','c')`` COLUMN_TYPE declaration."""
    inside = column_type[column_type.find("(") + 1:column_type.rfind(")")]
    labels: list[str] = []
    for raw in inside.split(","):
        s = raw.strip()
        if s.startswith("'") and s.endswith("'"):
            labels.append(s[1:-1].replace("''", "'"))
    return labels


def introspect(dsn: str, *, schema: str | None = None) -> IntrospectedSchema:
    """Read a live MySQL schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    db = _db_name(dsn, schema)
    conn = connect(dsn)
    try:
        cur = conn.cursor()

        def rows(sql: str) -> list[tuple]:
            cur.execute(sql, (db,))
            return list(cur.fetchall())

        tables: dict[str, IntrospectedTable] = {r[0]: IntrospectedTable(name=r[0]) for r in rows(_Q_TABLES)}
        enums: dict[str, IntrospectedEnum] = {}
        for (tname, cname, _ord, dtype, ctype, is_null, default, clen, nprec, nscale, extra) in rows(_Q_COLUMNS):
            if tname not in tables:
                continue
            dt = str(dtype).lower()
            col = IntrospectedColumn(
                name=cname, data_type=dt, column_type=ctype,
                nullable=(str(is_null).upper() == "YES"),
                default=default, char_max_length=clen,
                numeric_precision=nprec, numeric_scale=nscale,
                is_identity=("auto_increment" in str(extra or "").lower()),
            )
            if dt == "enum":
                # MySQL ENUM is inline (per column), not a shared type. Synthesise a deterministic
                # enum name so the *same* pure build path the Postgres importer uses picks it up.
                ename = f"{tname}_{cname}"
                enums[ename] = IntrospectedEnum(name=ename, labels=_parse_enum(str(ctype)))
                col.data_type = "user-defined"
                col.udt_name = ename
            tables[tname].columns.append(col)

        for (tname, cname, _ord) in rows(_Q_PRIMARY_KEYS):
            if tname in tables:
                tables[tname].primary_key.append(cname)

        # Key by (table, constraint), consistent with the other drivers and the index accumulation
        # below — never by constraint name alone, which would merge two same-named FKs on different
        # tables into one and drop a relation.
        fks: dict[tuple[str, str], IntrospectedForeignKey] = {}
        for (cstr, tname, col, _ord, ref_table, ref_col, del_rule, upd_rule) in rows(_Q_FOREIGN_KEYS):
            fk = fks.get((tname, cstr))
            if fk is None:
                fk = IntrospectedForeignKey(name=cstr, table=tname, ref_table=ref_table,
                                            on_delete=_rule(del_rule), on_update=_rule(upd_rule))
                fks[(tname, cstr)] = fk
            fk.columns.append(col)
            fk.ref_columns.append(ref_col)

        idx: dict[tuple[str, str], IntrospectedIndex] = {}
        for (tname, iname, non_unique, col, _seq) in rows(_Q_INDEXES):
            if iname == "PRIMARY":
                continue  # represented by the table's primary_key, not as an index
            key = (tname, iname)
            if key not in idx:
                idx[key] = IntrospectedIndex(name=iname, table=tname, unique=(int(non_unique) == 0))
            idx[key].columns.append(col)

        cur.close()
        return IntrospectedSchema(
            tables=sorted(tables.values(), key=lambda t: t.name),
            foreign_keys=sorted(fks.values(), key=lambda f: f.name),
            indexes=sorted(idx.values(), key=lambda i: (i.table, i.name)),
            enums=sorted(enums.values(), key=lambda e: e.name),
        )
    finally:
        conn.close()


def apply_sql(dsn: str, statements: list[str]) -> None:
    """Apply raw-SQL statements in order to a (shadow) MySQL database (impure)."""
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        for stmt in statements:
            s = stmt.strip()
            if s and not s.startswith("--"):
                cur.execute(s)
        cur.close()
    finally:
        conn.close()


def reset(dsn: str, *, schema: str | None = None) -> None:
    """Reset a shadow MySQL database by dropping every table (FK checks off so order doesn't matter)."""
    db = _db_name(dsn, schema)
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'", (db,))
        for (tname,) in list(cur.fetchall()):
            cur.execute(f"DROP TABLE IF EXISTS `{tname}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        cur.close()
    finally:
        conn.close()


# ==================================================================================================
# Reverse type map (native MySQL column → physical spec + semantic hint) — spec §1/§2.
# ==================================================================================================
_TEXT_TYPES = {"text", "tinytext", "mediumtext", "longtext"}


def column_physical(col: IntrospectedColumn) -> dict[str, Any]:
    dt = (col.data_type or "").strip().lower()
    ct = (col.column_type or "").strip().lower()
    if dt == "char":
        return {"type": "char", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt == "varchar":
        return {"type": "varchar", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt in _TEXT_TYPES:
        return {"type": "text"}
    if dt == "tinyint":
        return {"type": "tinyint", **({"length": 1} if ct.startswith("tinyint(1)") else {})}
    if dt == "smallint":
        return {"type": "smallint"}
    if dt in {"int", "integer", "mediumint"}:
        return {"type": "integer"}
    if dt == "bigint":
        return {"type": "bigint"}
    if dt in {"decimal", "numeric"}:
        spec: dict[str, Any] = {"type": "numeric"}
        if col.numeric_precision is not None:
            spec["precision"] = col.numeric_precision
            spec["scale"] = col.numeric_scale or 0
        return spec
    if dt in {"double", "double precision", "float", "real"}:
        return {"type": dt}
    if dt == "datetime":
        return {"type": "datetime"}
    if dt == "timestamp":
        return {"type": "timestamp"}
    if dt == "date":
        return {"type": "date"}
    if dt == "time":
        return {"type": "time"}
    if dt == "json":
        return {"type": "json"}
    if dt == "user-defined" and col.udt_name:
        return {"type": col.udt_name}  # an inline ENUM, synthesised into a named type by introspect
    return {"type": dt or "text"}


def is_autoincrement(col: IntrospectedColumn) -> bool:
    return col.is_identity  # set from the column's EXTRA='auto_increment' during introspection


def semantic_override(col: IntrospectedColumn, physical: dict[str, Any]) -> str | None:
    """MySQL has no native uuid — a ``CHAR(36)`` column is the canonical uuid mapping (the FK lesson,
    spec §1). Inferring it back to ``uuid`` is what closes the emit↔import round-trip."""
    if physical.get("type") == "char" and physical.get("length") == 36:
        return "uuid"
    return None
