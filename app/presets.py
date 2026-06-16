"""Field-type suggestions (Phase 6F enhancements — feature #12).

A catalog of common, ready-to-use column definitions the canvas offers when a user adds/edits a
column ("id (bigint PK)", "email (varchar UNIQUE)", "created_at (timestamp)", …). Each preset is a
plain `SchemaField`-compatible dict so the frontend can drop it straight into a table or pre-fill the
column editor. Kept deterministic and dependency-free so it is trivially unit-testable.
"""

from __future__ import annotations

# Each entry: a human label + a SchemaField payload. `category` groups them in the UI menu.
FIELD_PRESETS: list[dict] = [
    {"label": "id — bigint primary key", "category": "keys",
     "field": {"name": "id", "type": "bigint", "primary_key": True, "auto_increment": True, "nullable": False}},
    {"label": "uuid — UUID primary key", "category": "keys",
     "field": {"name": "id", "type": "uuid", "primary_key": True, "nullable": False}},
    {"label": "user_id — foreign key", "category": "keys",
     "field": {"name": "user_id", "type": "foreign_id", "indexed": True, "nullable": False}},

    {"label": "email — unique varchar", "category": "common",
     "field": {"name": "email", "type": "varchar", "length": 255, "unique": True, "nullable": False}},
    {"label": "password — varchar", "category": "common",
     "field": {"name": "password", "type": "varchar", "length": 255, "nullable": False}},
    {"label": "name — varchar", "category": "common",
     "field": {"name": "name", "type": "varchar", "length": 255, "nullable": False}},
    {"label": "title — varchar", "category": "common",
     "field": {"name": "title", "type": "varchar", "length": 255, "nullable": False}},
    {"label": "slug — unique varchar", "category": "common",
     "field": {"name": "slug", "type": "varchar", "length": 255, "unique": True, "indexed": True, "nullable": False}},
    {"label": "description — text", "category": "common",
     "field": {"name": "description", "type": "text", "nullable": True}},

    {"label": "is_active — boolean (default true)", "category": "flags",
     "field": {"name": "is_active", "type": "boolean", "default": True, "nullable": False}},
    {"label": "status — enum", "category": "flags",
     "field": {"name": "status", "type": "enum", "values": ["pending", "active", "completed"], "nullable": False}},

    {"label": "price — decimal(10,2)", "category": "numeric",
     "field": {"name": "price", "type": "decimal", "precision": 10, "scale": 2, "nullable": False}},
    {"label": "quantity — integer (default 0)", "category": "numeric",
     "field": {"name": "quantity", "type": "integer", "default": 0, "nullable": False}},

    {"label": "created_at — timestamp", "category": "timestamps",
     "field": {"name": "created_at", "type": "timestamp", "nullable": False}},
    {"label": "updated_at — timestamp", "category": "timestamps",
     "field": {"name": "updated_at", "type": "timestamp", "nullable": False}},
    {"label": "deleted_at — nullable timestamp", "category": "timestamps",
     "field": {"name": "deleted_at", "type": "timestamp", "nullable": True}},

    {"label": "metadata — json", "category": "misc",
     "field": {"name": "metadata", "type": "json", "nullable": True}},
]


def field_presets() -> list[dict]:
    """Return the common-column catalog for the canvas's field-type suggestions."""
    return FIELD_PRESETS
