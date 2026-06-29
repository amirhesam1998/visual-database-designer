"""Driver abstraction — the boundary contract every database plugs into (multi-driver milestone §0).

The engine and ``schema_json`` are driver-agnostic; only the three places that touch a *real* database
are driver-aware — **import** (introspection + reverse type map), **emit** (DDL dialect) and the
**connection**. This module defines the neutral pieces shared by every driver:

* :class:`IntrospectedSchema` & friends — the plain, JSON-able result of introspection (the seam
  between impure I/O and the pure :func:`importer.build_schema_json`).
* :class:`SqlDialect` — per-driver DDL syntax kept as *data/behaviour* (quoting, table options,
  index strategy, the few divergent ALTER shapes), never as ``if driver == …`` branches scattered
  through the emitter (the same approach :mod:`app.core.risk` already takes — spec §3).
* :class:`Driver` — bundles a dialect with the impure callables (introspect/apply/reset) and the
  reverse type map (native column → physical + semantic hint), so adding a database is a new module,
  not surgery on the Core (spec §0).

The whole-project lesson — *an FK column's type must equal the referenced PK's type* — is enforced
once, in the Type System (``resolve_fk_physical`` is driver-aware), so every driver inherits it for
free: a uuid PK is ``uuid`` on Postgres and ``CHAR(36)`` on MySQL, and the FK column follows.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.core.schema_json import Field_
from app.core.type_system import (
    DEFAULT_REGISTRY,
    TypeRegistry,
    UnsupportedPhysicalTypeError,
)


# ==================================================================================================
# Introspected data model (plain, JSON-able — the boundary between impure I/O and the pure build).
# ==================================================================================================
class IntrospectedColumn(BaseModel):
    name: str
    data_type: str  # canonical/native type name, e.g. "character varying", "integer", "USER-DEFINED"
    udt_name: str | None = None  # underlying type name (enum type name when data_type=USER-DEFINED)
    column_type: str | None = None  # full native declaration when it matters (MySQL "tinyint(1)", "enum(...)")
    nullable: bool = True
    default: str | None = None
    char_max_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    is_identity: bool = False


class IntrospectedTable(BaseModel):
    name: str
    columns: list[IntrospectedColumn] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    checks: list[dict[str, str]] = Field(default_factory=list)  # [{name, clause}]


class IntrospectedForeignKey(BaseModel):
    name: str
    table: str
    columns: list[str] = Field(default_factory=list)
    ref_table: str
    ref_columns: list[str] = Field(default_factory=list)
    on_delete: str | None = None
    on_update: str | None = None


class IntrospectedIndex(BaseModel):
    name: str
    table: str
    columns: list[str] = Field(default_factory=list)
    unique: bool = False


class IntrospectedEnum(BaseModel):
    name: str
    labels: list[str] = Field(default_factory=list)


class IntrospectedSchema(BaseModel):
    tables: list[IntrospectedTable] = Field(default_factory=list)
    foreign_keys: list[IntrospectedForeignKey] = Field(default_factory=list)
    indexes: list[IntrospectedIndex] = Field(default_factory=list)
    enums: list[IntrospectedEnum] = Field(default_factory=list)


# ==================================================================================================
# SQL dialect — per-driver DDL syntax as behaviour (quoting + the few divergent statement shapes).
# ==================================================================================================
def render_physical(p: dict[str, Any]) -> str:
    """Render a resolved physical spec dict (``{type, length?, precision?, scale?, dimension?}``)."""
    base = str(p.get("type", "text"))
    if "length" in p:
        return f"{base}({p['length']})"
    if "precision" in p:
        return f"{base}({p['precision']},{p.get('scale', 0)})"
    if "dimension" in p:
        return f"{base}({p['dimension']})"
    return base


class SqlDialect:
    """Base dialect. Subclasses set the quoting/options attributes and override the few statement
    shapes that genuinely differ between databases; everything shared lives here.
    """

    name: str = ""
    quote_char: str = '"'
    table_options: str = ""  # appended after the closing ``)`` of CREATE TABLE (e.g. " ENGINE=InnoDB")
    drop_table_clause: str = ""  # appended after DROP TABLE IF EXISTS <name> (e.g. " CASCADE")
    autoincrement_keyword: str = ""  # column attribute for self-incrementing keys (MySQL AUTO_INCREMENT)

    def q(self, identifier: str) -> str:
        """Quote a SQL identifier, doubling any embedded quote char."""
        qc = self.quote_char
        return qc + str(identifier).replace(qc, qc * 2) + qc

    def physical(self, field: Field_, reg: TypeRegistry) -> dict[str, Any]:
        try:
            return reg.resolve(field, self.name).physical
        except (KeyError, UnsupportedPhysicalTypeError):
            return {"type": "text"}  # unknown/unsupported semantic type → a column the DDL can still create

    def render_type(self, field: Field_, reg: TypeRegistry) -> str:
        """Default: render the resolved physical type verbatim (MySQL behaviour; Postgres adds serial)."""
        return render_physical(self.physical(field, reg))

    def create_table_sql(self, name: str, body: str) -> str:
        return f"CREATE TABLE {self.q(name)} (\n{body}\n){self.table_options};"

    def drop_table_sql(self, name: str) -> str:
        return f"DROP TABLE IF EXISTS {self.q(name)}{self.drop_table_clause};"

    # The following four diverge between engines and are overridden by each concrete dialect.
    def add_index_sql(self, iname: str, tname: str, cols: list[str], *, unique: bool) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def drop_index_sql(self, iname: str, tname: str) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def change_type_sql(self, tname: str, cname: str, new_type: str, old_type: str) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def set_not_null_sql(self, tname: str, cname: str, type_str: str, *, recommended: str | None = None
                         ) -> tuple[list[str], list[str], str | None]:
        raise NotImplementedError

    def drop_not_null_sql(self, tname: str, cname: str, type_str: str) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def drop_primary_key_sql(self, tname: str) -> str:
        raise NotImplementedError


# ==================================================================================================
# Driver — bundles a dialect with the impure callables and the reverse type map.
# ==================================================================================================
class Driver(Protocol):
    """The contract a database implements. The Type System owns the *forward* map (semantic →
    physical, per driver); a Driver owns the *reverse* map (native column → physical + semantic
    hint) plus the impure I/O (introspect/apply/reset) and the SQL dialect for emit."""

    name: str
    dialect: SqlDialect
    default_schema: str

    def introspect(self, dsn: str, *, schema: str | None = None) -> IntrospectedSchema: ...
    def apply_sql(self, dsn: str, statements: list[str]) -> None: ...
    def reset(self, dsn: str, *, schema: str | None = None) -> None: ...
    def column_physical(self, col: IntrospectedColumn) -> dict[str, Any]: ...
    def is_autoincrement(self, col: IntrospectedColumn) -> bool: ...
    def semantic_override(self, col: IntrospectedColumn, physical: dict[str, Any]) -> str | None: ...


__all__ = [
    "IntrospectedColumn", "IntrospectedTable", "IntrospectedForeignKey", "IntrospectedIndex",
    "IntrospectedEnum", "IntrospectedSchema", "SqlDialect", "Driver", "render_physical",
    "DEFAULT_REGISTRY",
]
