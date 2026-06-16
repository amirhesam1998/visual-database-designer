"""Schema versioning + diff → migration (Phase 6F enhancements — features #9 / #20).

The canvas keeps version snapshots client-side; this module provides the stateless backend it calls
to **compare** two schema versions and **generate a migration script** from the difference.

`compare_schemas` reuses `SchemaComparator.diff()` (added/removed/changed tables + fields).
`diff_to_migration` turns that diff into an ordered SQL `ALTER`/`CREATE`/`DROP` script using the same
type rendering as the SQL exporter, so the output is consistent with a from-scratch export.
"""

from __future__ import annotations

from app.comparators import SchemaComparator
from app.exporters import SQLExporter
from app.schema_model import DatabaseSchema


def compare_schemas(old: DatabaseSchema, new: DatabaseSchema) -> dict:
    return SchemaComparator(old, new).diff()


def diff_to_migration(old: DatabaseSchema, new: DatabaseSchema, diff: dict | None = None) -> str:
    """Render an ordered SQL migration that turns `old` into `new`."""
    diff = diff if diff is not None else compare_schemas(old, new)
    exporter = SQLExporter(new)
    lines: list[str] = ["-- Migration generated from schema diff", ""]

    # New tables — full CREATE TABLE (+ their FKs).
    for name in diff.get("added_tables", []):
        table = new.table(name)
        if table is not None:
            lines.append(exporter._export_table(table))
            for relation in table.relations:
                lines.append(exporter._export_foreign_key(table, relation))
            lines.append("")

    # Changed tables — ADD/DROP COLUMN.
    for change in diff.get("changed_tables", []):
        table_name = change["table"]
        new_table = new.table(table_name)
        for field_name in change.get("added_fields", []):
            field = new_table.field(field_name) if new_table else None
            if field is not None:
                single_pk = len(new_table.primary_keys()) == 1
                col = exporter._field_to_sql(field, single_pk_table=single_pk)
                lines.append(f"ALTER TABLE {table_name} ADD COLUMN {col};")
        for field_name in change.get("removed_fields", []):
            lines.append(f"ALTER TABLE {table_name} DROP COLUMN {field_name};")

    # Removed tables — DROP last so FKs unwind cleanly.
    for name in diff.get("removed_tables", []):
        lines.append(f"DROP TABLE IF EXISTS {name};")

    body = "\n".join(line for line in lines).strip()
    return body or "-- No changes between the two schema versions."


def empty_schema() -> DatabaseSchema:
    """A baseline (no tables) so a first version diffs as 'everything added'."""
    return DatabaseSchema(tables=[])
