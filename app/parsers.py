"""Schema import parsers (task 6F.1/6F.7 — `/import`).

`SQLParser` reads a `CREATE TABLE` dump; `LaravelMigrationParser` reads `Schema::create` migration
code. Both are intentionally lightweight (regex-based) — enough to bootstrap a design from an
existing database without a full SQL grammar. Anything they cannot parse is skipped rather than
raising, so a partial import still yields an editable schema.
"""

from __future__ import annotations

import re

from app.schema_model import DatabaseSchema, FieldType, SchemaField, Table

# CREATE TABLE [IF NOT EXISTS] `name` ( ... );  — capture name + body (balanced-ish via the last ")").
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?(?P<name>\w+)["`\]]?\s*\((?P<body>.*?)\)\s*;',
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_RE = re.compile(
    r'^\s*["`\[]?(?P<name>\w+)["`\]]?\s+(?P<type>[A-Za-z_]+)\s*(?:\((?P<args>[^)]*)\))?(?P<rest>.*)$'
)
_TABLE_CONSTRAINTS = ("primary", "foreign", "unique", "constraint", "key", "check", "index")


class SQLParser:
    """Parse a SQL `CREATE TABLE` dump into a `DatabaseSchema`."""

    def parse(self, sql: str, *, driver: str = "postgresql") -> DatabaseSchema:
        tables: list[Table] = []
        for match in _CREATE_TABLE_RE.finditer(sql or ""):
            tables.append(self._parse_table(match.group("name"), match.group("body")))
        return DatabaseSchema(id="imported", type="sql", driver=driver, tables=tables)

    def _parse_table(self, name: str, body: str) -> Table:
        fields: list[SchemaField] = []
        inline_pks: list[str] = []
        for raw_line in self._split_columns(body):
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith(_TABLE_CONSTRAINTS):
                if lowered.startswith("primary"):
                    inline_pks.extend(self._extract_paren_names(line))
                continue
            field = self._parse_column(line)
            if field:
                fields.append(field)
        for pk in inline_pks:
            target = next((f for f in fields if f.name == pk), None)
            if target:
                target.primary_key = True
                target.nullable = False
        return Table(name=name, fields=fields, timestamps=False)

    @staticmethod
    def _split_columns(body: str) -> list[str]:
        """Split the table body on commas that are not inside parentheses."""
        parts: list[str] = []
        depth = 0
        current = ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(current)
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current)
        return parts

    def _parse_column(self, line: str) -> SchemaField | None:
        m = _COLUMN_RE.match(line)
        if not m:
            return None
        rest = (m.group("rest") or "").lower()
        args = m.group("args") or ""
        field = SchemaField(
            name=m.group("name"),
            type=FieldType.coerce(m.group("type")),
            nullable="not null" not in rest,
            unique="unique" in rest,
            primary_key="primary key" in rest,
            auto_increment="auto_increment" in rest or "autoincrement" in rest or "serial" in m.group("type").lower(),
        )
        if field.primary_key:
            field.nullable = False
        if field.type == FieldType.VARCHAR and args.strip().isdigit():
            field.length = int(args.strip())
        if field.type == FieldType.DECIMAL and "," in args:
            prec, _, scale = args.partition(",")
            field.precision = int(prec.strip()) if prec.strip().isdigit() else None
            field.scale = int(scale.strip()) if scale.strip().isdigit() else None
        return field

    @staticmethod
    def _extract_paren_names(line: str) -> list[str]:
        inner = re.search(r"\(([^)]*)\)", line)
        if not inner:
            return []
        return [n.strip().strip('"`[]') for n in inner.group(1).split(",") if n.strip()]


_MIGRATION_TABLE_RE = re.compile(
    r"Schema::create\(\s*['\"](?P<name>\w+)['\"].*?function\s*\([^)]*\)\s*\{(?P<body>.*?)\}\s*\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_MIGRATION_COL_RE = re.compile(r"\$table->(?P<method>\w+)\(\s*['\"](?P<name>\w+)['\"](?P<args>[^;]*)")
_MIGRATION_ID_RE = re.compile(r"\$table->id\(\s*(?:['\"](?P<name>\w+)['\"])?\s*\)")

_LARAVEL_METHOD_MAP = {
    "id": FieldType.BIGINT,
    "bigincrements": FieldType.BIGINT,
    "biginteger": FieldType.BIGINT,
    "increments": FieldType.INTEGER,
    "integer": FieldType.INTEGER,
    "unsignedbiginteger": FieldType.BIGINT,
    "foreignid": FieldType.FOREIGN_ID,
    "string": FieldType.VARCHAR,
    "text": FieldType.TEXT,
    "boolean": FieldType.BOOLEAN,
    "decimal": FieldType.DECIMAL,
    "float": FieldType.DECIMAL,
    "date": FieldType.DATE,
    "datetime": FieldType.DATETIME,
    "timestamp": FieldType.TIMESTAMP,
    "json": FieldType.JSON,
    "uuid": FieldType.UUID,
    "enum": FieldType.ENUM,
}


class LaravelMigrationParser:
    """Parse a Laravel `Schema::create(...)` migration into a `DatabaseSchema`."""

    def parse(self, code: str, *, driver: str = "mysql") -> DatabaseSchema:
        tables: list[Table] = []
        for match in _MIGRATION_TABLE_RE.finditer(code or ""):
            tables.append(self._parse_table(match.group("name"), match.group("body")))
        return DatabaseSchema(id="imported", type="sql", driver=driver, tables=tables)

    def _parse_table(self, name: str, body: str) -> Table:
        fields: list[SchemaField] = []
        timestamps = "$table->timestamps(" in body
        soft_delete = "$table->softDeletes(" in body
        # Laravel's `$table->id()` / `$table->id('uuid')` declares the auto-increment PK. The bare,
        # argument-less form is not caught by the column regex (it needs a quoted name), so handle it
        # explicitly.
        id_match = _MIGRATION_ID_RE.search(body)
        if id_match:
            fields.append(
                SchemaField(name=id_match.group("name") or "id", type=FieldType.BIGINT,
                            primary_key=True, auto_increment=True, nullable=False)
            )
        for m in _MIGRATION_COL_RE.finditer(body):
            method = m.group("method").lower()
            if method in ("timestamps", "softdeletes", "remembertoken", "index", "foreign", "primary"):
                continue
            if method in ("id", "bigincrements", "increments") and any(f.primary_key for f in fields):
                continue  # already captured as the id() PK above
            ftype = _LARAVEL_METHOD_MAP.get(method, FieldType.VARCHAR)
            rest = m.group("args") or ""
            is_pk = method in ("id", "bigincrements", "increments")
            fields.append(
                SchemaField(
                    name=m.group("name"),
                    type=ftype,
                    primary_key=is_pk,
                    auto_increment=is_pk,
                    nullable=not is_pk and "->nullable(" in rest,
                    unique="->unique(" in rest,
                    indexed="->index(" in rest,
                )
            )
        return Table(name=name, fields=fields, timestamps=timestamps, soft_delete=soft_delete)


def parse_import(import_type: str, data: str) -> DatabaseSchema:
    if import_type in ("sql", "sql_dump"):
        return SQLParser().parse(data)
    if import_type in ("migration", "laravel"):
        return LaravelMigrationParser().parse(data)
    raise ValueError(f"unsupported import type: {import_type}")
