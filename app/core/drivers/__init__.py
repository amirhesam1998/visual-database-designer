"""Driver registry — the single extension point for databases (multi-driver milestone §0).

Adding a database (SQL Server, SQLite, …) is a new module under ``drivers/`` plus one line in
:data:`_DRIVERS`; the Core never changes. A :class:`Driver` bundles a database's three driver-aware
concerns — the SQL **dialect** (emit), **introspection** + reverse type map (import), and the
**connection** — behind one neutral object the emitter and importer consume.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.drivers import mysql as _mysql
from app.core.drivers import postgres as _postgres
from app.core.drivers import sqlserver as _sqlserver
from app.core.drivers.base import (
    Driver,
    IntrospectedColumn,
    IntrospectedEnum,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedSchema,
    IntrospectedTable,
    SqlDialect,
    render_physical,
)


@dataclass(frozen=True)
class _DriverImpl:
    """Concrete :class:`Driver` assembled from a module's functions (kept as data — spec §0/§3)."""

    name: str
    dialect: SqlDialect
    default_schema: str
    _introspect: Callable[..., IntrospectedSchema]
    _apply_sql: Callable[[str, list[str]], None]
    _reset: Callable[..., None]
    _column_physical: Callable[[IntrospectedColumn], dict[str, Any]]
    _is_autoincrement: Callable[[IntrospectedColumn], bool]
    _semantic_override: Callable[[IntrospectedColumn, dict[str, Any]], str | None]

    def introspect(self, dsn: str, *, schema: str | None = None) -> IntrospectedSchema:
        return self._introspect(dsn, schema=schema) if schema is not None else self._introspect(dsn)

    def apply_sql(self, dsn: str, statements: list[str]) -> None:
        self._apply_sql(dsn, statements)

    def reset(self, dsn: str, *, schema: str | None = None) -> None:
        self._reset(dsn, schema=schema) if schema is not None else self._reset(dsn)

    def column_physical(self, col: IntrospectedColumn) -> dict[str, Any]:
        return self._column_physical(col)

    def is_autoincrement(self, col: IntrospectedColumn) -> bool:
        return self._is_autoincrement(col)

    def semantic_override(self, col: IntrospectedColumn, physical: dict[str, Any]) -> str | None:
        return self._semantic_override(col, physical)


_DRIVERS: dict[str, _DriverImpl] = {
    "postgres": _DriverImpl(
        name="postgres", dialect=_postgres.PostgresDialect(), default_schema="public",
        _introspect=_postgres.introspect, _apply_sql=_postgres.apply_sql, _reset=_postgres.reset,
        _column_physical=_postgres.column_physical, _is_autoincrement=_postgres.is_autoincrement,
        _semantic_override=_postgres.semantic_override,
    ),
    "mysql": _DriverImpl(
        name="mysql", dialect=_mysql.MySqlDialect(), default_schema="",
        _introspect=_mysql.introspect, _apply_sql=_mysql.apply_sql, _reset=_mysql.reset,
        _column_physical=_mysql.column_physical, _is_autoincrement=_mysql.is_autoincrement,
        _semantic_override=_mysql.semantic_override,
    ),
    "sqlserver": _DriverImpl(
        name="sqlserver", dialect=_sqlserver.SqlServerDialect(), default_schema="dbo",
        _introspect=_sqlserver.introspect, _apply_sql=_sqlserver.apply_sql, _reset=_sqlserver.reset,
        _column_physical=_sqlserver.column_physical, _is_autoincrement=_sqlserver.is_autoincrement,
        _semantic_override=_sqlserver.semantic_override,
    ),
}

# MariaDB speaks the MySQL dialect; ``mssql`` is the common short name for SQL Server — alias both.
_ALIASES = {"mariadb": "mysql", "postgresql": "postgres", "mssql": "sqlserver"}

SUPPORTED_DRIVERS: tuple[str, ...] = ("postgres", "mysql", "sqlserver")


def get_driver(name: str | None) -> _DriverImpl:
    key = _ALIASES.get((name or "postgres").lower(), (name or "postgres").lower())
    if key not in _DRIVERS:
        raise ValueError(f"unsupported driver {name!r}; supported: {SUPPORTED_DRIVERS}")
    return _DRIVERS[key]


def get_dialect(name: str | None) -> SqlDialect:
    return get_driver(name).dialect


__all__ = [
    "Driver", "SqlDialect", "SUPPORTED_DRIVERS", "get_driver", "get_dialect", "render_physical",
    "IntrospectedColumn", "IntrospectedTable", "IntrospectedForeignKey", "IntrospectedIndex",
    "IntrospectedEnum", "IntrospectedSchema",
]
