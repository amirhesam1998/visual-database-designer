"""Export engines (task 6F.4).

Turn a `DatabaseSchema` into concrete artifacts:

  * `SQLExporter`             — CREATE TABLE + ALTER TABLE … FOREIGN KEY
  * `LaravelMigrationExporter`— a Laravel 11 anonymous-class migration
  * `PrismaExporter`          — a Prisma schema
  * `MermaidExporter`         — an ERD (`erDiagram`)
  * `OpenAPIExporter`         — an OpenAPI 3.0 stub (components.schemas + CRUD paths)

`export_all()` runs every exporter and returns the `{sql, migration, prisma, mermaid, openapi}` map
that lands in the module output's `exports` block.
"""

from __future__ import annotations

import json

from app.schema_model import DatabaseSchema, FieldType, Index, Relation, RelationType, SchemaField, Table


def _pascal(snake: str) -> str:
    return "".join(word[:1].upper() + word[1:] for word in (snake or "").split("_") if word)


def _sql_escape(text: str) -> str:
    return (text or "").replace("'", "''")


def _php_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SQL_AUTO_INCREMENT = {"postgresql": "", "mysql": " AUTO_INCREMENT", "sqlite": " AUTOINCREMENT"}


class SQLExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        statements: list[str] = [self._export_table(t) for t in self.schema.tables]
        for table in self.schema.tables:
            for relation in table.relations:
                statements.append(self._export_foreign_key(table, relation))
        for table in self.schema.tables:
            for index in table.indexes:
                statements.append(self._export_index(table, index))
        for table in self.schema.tables:
            statements.extend(self._export_comments(table))
        return "\n\n".join(s for s in statements if s)

    def _export_index(self, table: Table, index: Index) -> str:
        if not index.columns:
            return ""
        name = index.resolved_name(table.name)
        cols = ", ".join(index.columns)
        if index.type == "fulltext" and self.schema.driver == "postgresql":
            expr = " || ' ' || ".join(f"coalesce({c}, '')" for c in index.columns)
            return f"CREATE INDEX {name} ON {table.name} USING GIN (to_tsvector('english', {expr}));"
        unique = "UNIQUE " if index.unique else ""
        return f"CREATE {unique}INDEX {name} ON {table.name} ({cols});"

    def _export_comments(self, table: Table) -> list[str]:
        out: list[str] = []
        if table.description:
            out.append(f"COMMENT ON TABLE {table.name} IS '{_sql_escape(table.description)}';")
        for field in table.fields:
            if field.description:
                out.append(
                    f"COMMENT ON COLUMN {table.name}.{field.name} IS '{_sql_escape(field.description)}';"
                )
        return out

    def _export_table(self, table: Table) -> str:
        pk_count = sum(1 for f in table.fields if f.primary_key)
        lines = [self._field_to_sql(f, single_pk_table=pk_count == 1) for f in table.fields]
        # Single auto-increment PK is declared inline; composite PKs as a table constraint.
        if pk_count > 1:
            pk_fields = [f.name for f in table.fields if f.primary_key]
            lines.append(f"PRIMARY KEY ({', '.join(pk_fields)})")
        body = ",\n  ".join(lines)
        return f"CREATE TABLE {table.name} (\n  {body}\n);"

    def _field_to_sql(self, field: SchemaField, *, single_pk_table: bool) -> str:
        inline_pk = field.primary_key and single_pk_table
        parts = [field.name, self._type_to_sql(field)]
        if inline_pk:
            parts.append("PRIMARY KEY")
        if field.auto_increment:
            ai = _SQL_AUTO_INCREMENT.get(self.schema.driver, "")
            if self.schema.driver == "postgresql":
                # Postgres uses SERIAL/BIGSERIAL types instead of an AUTO_INCREMENT keyword.
                parts[1] = "BIGSERIAL" if field.type == FieldType.BIGINT else "SERIAL"
            elif ai:
                parts.append(ai.strip())
        if not field.nullable and not inline_pk:
            parts.append("NOT NULL")
        if field.unique and not inline_pk:
            parts.append("UNIQUE")
        if field.default is not None:
            parts.append(f"DEFAULT {self._default_to_sql(field)}")
        return " ".join(parts)

    def _type_to_sql(self, field: SchemaField) -> str:
        t = field.type
        if t == FieldType.VARCHAR:
            return f"VARCHAR({field.length or 255})"
        if t == FieldType.DECIMAL:
            return f"DECIMAL({field.precision or 10},{field.scale or 2})"
        if t == FieldType.ENUM and field.values:
            opts = ", ".join(f"'{v}'" for v in field.values)
            return f"VARCHAR(255) CHECK ({field.name} IN ({opts}))"
        if t in (FieldType.UUID,):
            return "UUID" if self.schema.driver == "postgresql" else "VARCHAR(36)"
        if t == FieldType.FOREIGN_ID:
            return "BIGINT"
        if t == FieldType.JSON:
            return "JSONB" if self.schema.driver == "postgresql" else "JSON"
        if t == FieldType.DATETIME:
            return "TIMESTAMP"
        return t.value.upper()

    @staticmethod
    def _default_to_sql(field: SchemaField) -> str:
        value = field.default
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if text.upper() in ("CURRENT_TIMESTAMP", "NOW()", "NULL"):
            return text
        return f"'{text}'"

    def _export_foreign_key(self, table: Table, relation: Relation) -> str:
        if relation.type == RelationType.MANY_TO_MANY:
            return ""  # pivot tables are modeled as their own tables, not FK columns
        col = relation.from_field or f"{relation.to_table}_id"
        return (
            f"ALTER TABLE {table.name} ADD CONSTRAINT fk_{table.name}_{col}\n"
            f"  FOREIGN KEY ({col}) REFERENCES {relation.to_table}({relation.to_field or 'id'})\n"
            f"  ON DELETE {relation.on_delete.upper().replace('_', ' ')} "
            f"ON UPDATE {relation.on_update.upper().replace('_', ' ')};"
        )


# ---------------------------------------------------------------------------
# Laravel migration
# ---------------------------------------------------------------------------

_LARAVEL_TYPE_MAP = {
    FieldType.BIGINT: "bigInteger",
    FieldType.INTEGER: "integer",
    FieldType.VARCHAR: "string",
    FieldType.TEXT: "text",
    FieldType.BOOLEAN: "boolean",
    FieldType.DECIMAL: "decimal",
    FieldType.DATE: "date",
    FieldType.DATETIME: "dateTime",
    FieldType.TIMESTAMP: "timestamp",
    FieldType.JSON: "json",
    FieldType.UUID: "uuid",
    FieldType.ENUM: "enum",
    FieldType.FOREIGN_ID: "foreignId",
}


class LaravelMigrationExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        head = (
            "<?php\n\n"
            "use Illuminate\\Database\\Migrations\\Migration;\n"
            "use Illuminate\\Database\\Schema\\Blueprint;\n"
            "use Illuminate\\Support\\Facades\\Schema;\n\n"
            "return new class extends Migration\n{\n"
            "    public function up(): void\n    {\n"
        )
        up = "".join(self._export_table(t) for t in self.schema.tables)
        mid = "    }\n\n    public function down(): void\n    {\n"
        down = "".join(
            f"        Schema::dropIfExists('{t.name}');\n" for t in reversed(self.schema.tables)
        )
        tail = "    }\n};\n"
        return head + up + mid + down + tail

    def _export_table(self, table: Table) -> str:
        code = f"        Schema::create('{table.name}', function (Blueprint $table) {{\n"
        for field in table.fields:
            code += self._field_to_migration(field)
        # Composite primary key (single auto-increment PK is emitted inline as ->id()).
        pks = table.primary_keys()
        if len(pks) > 1:
            cols = ", ".join(f"'{p}'" for p in pks)
            code += f"            $table->primary([{cols}]);\n"
        for index in table.indexes:
            code += self._index_to_migration(index)
        if table.timestamps:
            code += "            $table->timestamps();\n"
        if table.soft_delete:
            code += "            $table->softDeletes();\n"
        if table.description:
            code += f"            $table->comment('{_php_escape(table.description)}');\n"
        code += "        });\n\n"
        return code

    def _index_to_migration(self, index: Index) -> str:
        if not index.columns:
            return ""
        cols = ", ".join(f"'{c}'" for c in index.columns)
        method = "fullText" if index.type == "fulltext" else ("unique" if index.unique else "index")
        name_arg = f", '{index.name}'" if index.name else ""
        return f"            $table->{method}([{cols}]{name_arg});\n"

    def _field_to_migration(self, field: SchemaField) -> str:
        # Auto-increment integer PK → Laravel's id()/bigIncrements helper.
        if field.primary_key and field.auto_increment and field.type in (FieldType.BIGINT, FieldType.INTEGER):
            return f"            $table->id('{field.name}');\n"

        method = _LARAVEL_TYPE_MAP.get(field.type, "string")
        if field.type == FieldType.VARCHAR and field.length:
            args = f"'{field.name}', {field.length}"
        elif field.type == FieldType.DECIMAL:
            args = f"'{field.name}', {field.precision or 10}, {field.scale or 2}"
        elif field.type == FieldType.ENUM and field.values:
            opts = ", ".join(f"'{v}'" for v in field.values)
            args = f"'{field.name}', [{opts}]"
        else:
            args = f"'{field.name}'"

        code = f"            $table->{method}({args})"
        if field.nullable and not field.primary_key:
            code += "->nullable()"
        if field.unique:
            code += "->unique()"
        if field.indexed and not field.unique and not field.primary_key:
            code += "->index()"
        if field.default is not None:
            code += f"->default({self._laravel_default(field)})"
        if field.description:
            code += f"->comment('{_php_escape(field.description)}')"
        code += ";\n"
        return code

    @staticmethod
    def _laravel_default(field: SchemaField) -> str:
        value = field.default
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return f"'{value}'"


# ---------------------------------------------------------------------------
# Prisma
# ---------------------------------------------------------------------------

_PRISMA_TYPE_MAP = {
    FieldType.BIGINT: "BigInt",
    FieldType.INTEGER: "Int",
    FieldType.VARCHAR: "String",
    FieldType.TEXT: "String",
    FieldType.BOOLEAN: "Boolean",
    FieldType.DECIMAL: "Decimal",
    FieldType.DATE: "DateTime",
    FieldType.DATETIME: "DateTime",
    FieldType.TIMESTAMP: "DateTime",
    FieldType.JSON: "Json",
    FieldType.UUID: "String",
    FieldType.FOREIGN_ID: "BigInt",
    FieldType.ENUM: "String",
}


class PrismaExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        out = [
            "// Generated by Visual Database Designer",
            "",
            "datasource db {",
            f'  provider = "{self._driver_to_prisma()}"',
            '  url      = env("DATABASE_URL")',
            "}",
            "",
            "generator client {",
            '  provider = "prisma-client-js"',
            "}",
            "",
        ]
        for table in self.schema.tables:
            out.append(self._export_model(table))
            out.append("")
        return "\n".join(out)

    def _driver_to_prisma(self) -> str:
        return {
            "postgresql": "postgresql",
            "mysql": "mysql",
            "mongodb": "mongodb",
            "sqlite": "sqlite",
        }.get(self.schema.driver, "postgresql")

    def _export_model(self, table: Table) -> str:
        composite_pk = len(table.primary_keys()) > 1
        lines = [f"model {_pascal(table.name)} {{"]
        for field in table.fields:
            lines.append(self._field_to_prisma(field, composite_pk=composite_pk))
        if composite_pk:
            cols = ", ".join(table.primary_keys())
            lines.append(f"  @@id([{cols}])")
        for index in table.indexes:
            if not index.columns:
                continue
            cols = ", ".join(index.columns)
            lines.append(f"  @@{'unique' if index.unique else 'index'}([{cols}])")
        lines.append(f'  @@map("{table.name}")')
        lines.append("}")
        return "\n".join(lines)

    def _field_to_prisma(self, field: SchemaField, *, composite_pk: bool = False) -> str:
        type_name = _PRISMA_TYPE_MAP.get(field.type, "String")
        optional = "?" if field.nullable and not field.primary_key else ""
        code = f"  {field.name} {type_name}{optional}"
        attrs = []
        if field.primary_key and not composite_pk:
            attrs.append("@id")
            if field.auto_increment:
                attrs.append("@default(autoincrement())")
        if field.unique and not field.primary_key:
            attrs.append("@unique")
        if attrs:
            code += " " + " ".join(attrs)
        return code


# ---------------------------------------------------------------------------
# Mermaid ERD
# ---------------------------------------------------------------------------

_MERMAID_CARDINALITY = {
    RelationType.ONE_TO_ONE: "||--||",
    RelationType.ONE_TO_MANY: "||--o{",
    RelationType.MANY_TO_ONE: "}o--||",
    RelationType.MANY_TO_MANY: "}o--o{",
    RelationType.POLYMORPHIC: "}o--o{",
}


class MermaidExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        lines = ["erDiagram"]
        for table in self.schema.tables:
            lines.append(f"    {table.name} {{")
            for field in table.fields:
                marker = " PK" if field.primary_key else (" FK" if field.name.endswith("_id") else "")
                lines.append(f"        {field.type.value} {field.name}{marker}")
            lines.append("    }")
        for table in self.schema.tables:
            for relation in table.relations:
                card = _MERMAID_CARDINALITY.get(relation.type, "||--o{")
                label = relation.name or relation.from_field or "has"
                lines.append(f'    {relation.from_table} {card} {relation.to_table} : "{label}"')
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAPI stub
# ---------------------------------------------------------------------------

_OPENAPI_TYPE_MAP = {
    FieldType.BIGINT: ("integer", "int64"),
    FieldType.INTEGER: ("integer", "int32"),
    FieldType.FOREIGN_ID: ("integer", "int64"),
    FieldType.DECIMAL: ("number", None),
    FieldType.NUMBER: ("number", None),
    FieldType.BOOLEAN: ("boolean", None),
    FieldType.DATE: ("string", "date"),
    FieldType.DATETIME: ("string", "date-time"),
    FieldType.TIMESTAMP: ("string", "date-time"),
    FieldType.UUID: ("string", "uuid"),
    FieldType.JSON: ("object", None),
    FieldType.OBJECT: ("object", None),
    FieldType.ARRAY: ("array", None),
}


class OpenAPIExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Generated API", "version": "0.1.0"},
            "paths": {},
            "components": {"schemas": {}},
        }
        for table in self.schema.tables:
            model = _pascal(table.name)
            spec["components"]["schemas"][model] = self._table_schema(table)
            spec["paths"].update(self._table_paths(table, model))
        return json.dumps(spec, indent=2)

    def _table_schema(self, table: Table) -> dict:
        properties: dict[str, dict] = {}
        required: list[str] = []
        for field in table.fields:
            otype, fmt = _OPENAPI_TYPE_MAP.get(field.type, ("string", None))
            prop: dict = {"type": otype}
            if fmt:
                prop["format"] = fmt
            if field.type == FieldType.ENUM and field.values:
                prop["enum"] = field.values
            properties[field.name] = prop
            if not field.nullable:
                required.append(field.name)
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _table_paths(self, table: Table, model: str) -> dict:
        ref = {"$ref": f"#/components/schemas/{model}"}
        collection = f"/{table.name}"
        item = f"/{table.name}/{{id}}"
        return {
            collection: {
                "get": {"summary": f"List {table.name}", "responses": {
                    "200": {"description": "OK", "content": {"application/json": {
                        "schema": {"type": "array", "items": ref}}}}}},
                "post": {"summary": f"Create {model}", "requestBody": {"content": {
                    "application/json": {"schema": ref}}}, "responses": {"201": {"description": "Created"}}},
            },
            item: {
                "get": {"summary": f"Get {model}", "parameters": [self._id_param()],
                        "responses": {"200": {"description": "OK", "content": {
                            "application/json": {"schema": ref}}}}},
                "put": {"summary": f"Update {model}", "parameters": [self._id_param()],
                        "requestBody": {"content": {"application/json": {"schema": ref}}},
                        "responses": {"200": {"description": "Updated"}}},
                "delete": {"summary": f"Delete {model}", "parameters": [self._id_param()],
                           "responses": {"204": {"description": "Deleted"}}},
            },
        }

    @staticmethod
    def _id_param() -> dict:
        return {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


# ---------------------------------------------------------------------------
# Markdown data dictionary (Phase 3 #16 — comments / documentation)
# ---------------------------------------------------------------------------


class MarkdownDocExporter:
    """A human-readable data dictionary: tables, columns (type/constraints/comment), relations,
    indexes and reusable enums."""

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        out = ["# Data Dictionary", ""]
        for table in self.schema.tables:
            out.append(f"## {table.name}")
            if table.description:
                out.append(f"\n{table.description}")
            if table.group:
                out.append(f"\n*Group: {table.group}*")
            out += ["", "| Column | Type | Constraints | Description |",
                    "|--------|------|-------------|-------------|"]
            for f in table.fields:
                out.append(
                    f"| {f.name} | {self._type(f)} | {self._constraints(f)} | {f.description or ''} |"
                )
            if table.indexes:
                out.append("\n**Indexes:** " + ", ".join(
                    f"{i.resolved_name(table.name)} ({', '.join(i.columns)}{', unique' if i.unique else ''}"
                    f"{', fulltext' if i.type == 'fulltext' else ''})" for i in table.indexes))
            if table.relations:
                out.append("\n**Relations:** " + ", ".join(
                    f"{r.type} → {r.to_table}" for r in table.relations))
            out.append("")
        if self.schema.enums:
            out += ["## Enums", ""]
            for en in self.schema.enums:
                out.append(f"- **{en.name}**: {', '.join(en.values)}")
            out.append("")
        return "\n".join(out)

    @staticmethod
    def _type(f: SchemaField) -> str:
        if f.type == FieldType.VARCHAR and f.length:
            return f"varchar({f.length})"
        if f.type == FieldType.DECIMAL:
            return f"decimal({f.precision or 10},{f.scale or 2})"
        if f.type == FieldType.ENUM:
            return f"enum({', '.join(f.values or [])})" if f.values else "enum"
        return f.type.value

    @staticmethod
    def _constraints(f: SchemaField) -> str:
        c = []
        if f.primary_key:
            c.append("PK")
        if f.auto_increment:
            c.append("auto")
        if f.unique and not f.primary_key:
            c.append("unique")
        if f.indexed and not f.unique:
            c.append("index")
        c.append("null" if f.nullable else "not null")
        if f.default is not None:
            c.append(f"default {f.default}")
        return ", ".join(c)


# ---------------------------------------------------------------------------
# Façade
# ---------------------------------------------------------------------------

EXPORTERS = {
    "sql": SQLExporter,
    "migration": LaravelMigrationExporter,
    "prisma": PrismaExporter,
    "mermaid": MermaidExporter,
    "openapi": OpenAPIExporter,
    "markdown": MarkdownDocExporter,
}


def export_one(schema: DatabaseSchema, export_type: str) -> str:
    exporter_cls = EXPORTERS.get(export_type)
    if exporter_cls is None:
        raise ValueError(f"unknown export type: {export_type}")
    return exporter_cls(schema).export()


def export_all(schema: DatabaseSchema) -> dict[str, str]:
    return {name: cls(schema).export() for name, cls in EXPORTERS.items()}
