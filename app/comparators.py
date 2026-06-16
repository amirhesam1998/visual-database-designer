"""Schema diffing (task 6F.1 — `comparators.py`).

`SchemaComparator` reports the structural delta between two schema versions: added/removed tables
and, for tables present in both, added/removed/changed fields. Powers version history and
"what changed?" summaries.
"""

from __future__ import annotations

from app.schema_model import DatabaseSchema, SchemaField, Table


def _field_signature(field: SchemaField) -> tuple:
    return (field.type.value, field.nullable, field.unique, field.primary_key, field.length)


class SchemaComparator:
    def __init__(self, old: DatabaseSchema, new: DatabaseSchema):
        self.old = old
        self.new = new

    def diff(self) -> dict:
        old_tables = {t.name: t for t in self.old.tables}
        new_tables = {t.name: t for t in self.new.tables}

        added = sorted(new_tables.keys() - old_tables.keys())
        removed = sorted(old_tables.keys() - new_tables.keys())
        changed = [
            self._table_diff(old_tables[name], new_tables[name])
            for name in sorted(old_tables.keys() & new_tables.keys())
            if self._table_changed(old_tables[name], new_tables[name])
        ]
        return {
            "added_tables": added,
            "removed_tables": removed,
            "changed_tables": changed,
            "summary": self._summary(added, removed, changed),
        }

    def _table_changed(self, old: Table, new: Table) -> bool:
        return self._table_diff(old, new)["has_changes"]

    @staticmethod
    def _table_diff(old: Table, new: Table) -> dict:
        old_fields = {f.name: f for f in old.fields}
        new_fields = {f.name: f for f in new.fields}
        added = sorted(new_fields.keys() - old_fields.keys())
        removed = sorted(old_fields.keys() - new_fields.keys())
        modified = [
            name
            for name in sorted(old_fields.keys() & new_fields.keys())
            if _field_signature(old_fields[name]) != _field_signature(new_fields[name])
        ]
        return {
            "table": new.name,
            "added_fields": added,
            "removed_fields": removed,
            "modified_fields": modified,
            "has_changes": bool(added or removed or modified),
        }

    @staticmethod
    def _summary(added: list[str], removed: list[str], changed: list[dict]) -> str:
        parts = []
        if added:
            parts.append(f"{len(added)} table(s) added")
        if removed:
            parts.append(f"{len(removed)} table(s) removed")
        if changed:
            parts.append(f"{len(changed)} table(s) modified")
        return ", ".join(parts) or "No structural changes"
