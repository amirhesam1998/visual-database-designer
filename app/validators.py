"""Schema validation (task 6F.3).

`SchemaValidator` produces a `{valid, errors, warnings}` report. Errors block a "valid" schema;
warnings are advisory (naming, missing indexes, …). It is deterministic and LLM-free so the canvas
can validate on every edit in well under a second.
"""

from __future__ import annotations

import re

from app.schema_model import DatabaseSchema, Table

_RESERVED_WORDS = frozenset(
    {"select", "insert", "update", "delete", "from", "where", "table", "order", "group", "user",
     "index", "primary", "key", "column", "join", "default", "check", "constraint"}
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchemaValidator:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def validate(self) -> dict:
        """Run all validation checks and return a JSON-friendly report."""
        self.errors = []
        self.warnings = []
        self._check_has_tables()
        self._check_table_names()
        self._check_duplicate_tables()
        self._check_fields()
        self._check_primary_keys()
        self._check_relations()
        self._check_indexes()
        self._check_naming_conventions()
        return {
            "valid": len(self.errors) == 0,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    # -- checks -------------------------------------------------------------

    def _check_has_tables(self) -> None:
        if not self.schema.tables:
            self.errors.append("Schema has no tables.")

    def _check_table_names(self) -> None:
        for table in self.schema.tables:
            if not table.name:
                self.errors.append("A table has no name.")
                continue
            if not _IDENTIFIER_RE.match(table.name):
                self.errors.append(f"Table '{table.name}' is not a valid identifier.")
            if len(table.name) > 64:
                self.errors.append(f"Table name too long (>64 chars): {table.name}")
            if table.name.lower() in _RESERVED_WORDS:
                self.warnings.append(f"Table '{table.name}' is a reserved SQL word — consider renaming.")

    def _check_duplicate_tables(self) -> None:
        seen: set[str] = set()
        for table in self.schema.tables:
            key = table.name.lower()
            if key in seen:
                self.errors.append(f"Duplicate table name: {table.name}")
            seen.add(key)

    def _check_fields(self) -> None:
        for table in self.schema.tables:
            if not table.fields:
                self.errors.append(f"Table '{table.name}' has no fields.")
            seen: set[str] = set()
            for field in table.fields:
                if field.name.lower() in seen:
                    self.errors.append(f"Table '{table.name}' has a duplicate field '{field.name}'.")
                seen.add(field.name.lower())
                if not _IDENTIFIER_RE.match(field.name or ""):
                    self.errors.append(f"Field '{table.name}.{field.name}' is not a valid identifier.")

    def _check_primary_keys(self) -> None:
        for table in self.schema.tables:
            pk_count = sum(1 for f in table.fields if f.primary_key)
            if pk_count == 0:
                self.errors.append(f"Table '{table.name}' has no primary key.")
            if pk_count > 1:
                self.warnings.append(
                    f"Table '{table.name}' has {pk_count} primary-key columns (composite key) — confirm intentional."
                )

    def _check_relations(self) -> None:
        table_names = {t.name for t in self.schema.tables}
        for table in self.schema.tables:
            for relation in table.relations:
                if relation.to_table not in table_names:
                    self.errors.append(
                        f"Relation '{relation.name or relation.from_field}': target table "
                        f"'{relation.to_table}' not found."
                    )
                if relation.from_field and not table.field(relation.from_field):
                    self.warnings.append(
                        f"Relation on '{table.name}': field '{relation.from_field}' is not declared on the table."
                    )
                target = self.schema.table(relation.to_table)
                if target and relation.to_field and not target.field(relation.to_field):
                    self.warnings.append(
                        f"Relation '{relation.name or relation.from_field}': target column "
                        f"'{relation.to_table}.{relation.to_field}' not found."
                    )

    def _check_indexes(self) -> None:
        for table in self.schema.tables:
            for field in table.fields:
                if field.name.endswith("_id") and not field.indexed and not field.primary_key:
                    self.warnings.append(
                        f"Table '{table.name}': foreign-key field '{field.name}' should be indexed."
                    )
                if field.unique and not field.indexed:
                    self.warnings.append(
                        f"Table '{table.name}': unique field '{field.name}' should be indexed."
                    )

    def _check_naming_conventions(self) -> None:
        for table in self.schema.tables:
            self._check_table_naming(table)
            for field in table.fields:
                if field.name != field.name.lower():
                    self.warnings.append(f"Field '{table.name}.{field.name}' should be lowercase (snake_case).")

    def _check_table_naming(self, table: Table) -> None:
        if table.name and table.name != table.name.lower():
            self.warnings.append(f"Table '{table.name}' should be lowercase (snake_case).")
