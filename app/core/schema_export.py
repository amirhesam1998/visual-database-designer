"""Deterministic text exports from a ``schema_json`` (export-formats milestone §1).

These are documentation/interchange artifacts — YAML, DBML (dbdiagram.io), JSON Schema and a
Markdown **data dictionary** — added to the Code panel's Artifact menu beside SQL/OpenAPI. They follow
the project's golden rule: generation lives in the engine, is byte-for-byte deterministic (no LLM),
and reads from the layered ``schema_json`` which is the source of truth.

The one thing that matters most: types are **resolved through the Type System**, not taken raw, so a
foreign-key column shows the *referenced primary key's* physical type — a uuid FK is ``uuid``, never
an integer (the lesson of the whole project). Everything else (relations, indexes, enums, PII marks)
is read directly off the resolved model.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from app.core.schema_json import SchemaJson
from app.core.type_system import DEFAULT_REGISTRY, resolve_fk_physical

SUPPORTED_KINDS = ("yaml", "dbml", "jsonschema", "datadict")
_LANGUAGE = {"yaml": "yaml", "dbml": "dbml", "jsonschema": "json", "datadict": "markdown"}


# --------------------------------------------------------------------------------------------------
# Resolved model — one shared, deterministic projection every format renders from. Mirrors the canvas
# render projection (routes `_render_model`) so a FK column inherits its referenced PK's physical type.
# --------------------------------------------------------------------------------------------------
def _physical_label(physical: dict[str, Any]) -> str:
    base = str(physical.get("type", ""))
    if physical.get("length"):
        return f"{base}({physical['length']})"
    if physical.get("precision") is not None:
        return f"{base}({physical['precision']},{physical.get('scale', 0)})"
    if physical.get("dimension"):
        return f"{base}({physical['dimension']})"
    return base


def _resolved(schema: SchemaJson) -> dict[str, Any]:
    fk_physical = resolve_fk_physical(schema)
    fk_field_ids = {r.foreign_key_field_id for r in schema.logical.relations if r.foreign_key_field_id}

    # fk field id → (referenced table name, referenced pk field name) for DBML refs + the dictionary.
    references: dict[str, tuple[str, str]] = {}
    for rel in schema.logical.relations:
        if not rel.foreign_key_field_id or not rel.to_table_id:
            continue
        to_table = schema.table_by_id(rel.to_table_id)
        if not to_table:
            continue
        pks = to_table.primary_keys()
        references[rel.foreign_key_field_id] = (to_table.name, pks[0].name if pks else "id")

    enum_names = {e.id: e.name for e in schema.logical.enums}

    tables: list[dict[str, Any]] = []
    for table in schema.logical.tables:
        fields = []
        for field in table.fields:
            physical: dict[str, Any]
            pii = False
            sensitivity = None
            try:
                resolved = DEFAULT_REGISTRY.resolve(field)
                physical = dict(resolved.physical)
                pii = bool(resolved.privacy.get("pii"))
                sensitivity = resolved.privacy.get("sensitivity")
            except KeyError:
                physical = {"type": field.semantic_type}  # unregistered → show as-is
            if field.id in fk_physical:  # FK column takes the referenced PK's physical type
                physical = dict(fk_physical[field.id])
            fields.append({
                "id": field.id,
                "name": field.name,
                "semantic_type": field.semantic_type,
                "physical": physical,
                "physical_label": _physical_label(physical),
                "nullable": field.nullable and not field.is_primary_key,
                "is_primary_key": field.is_primary_key,
                "is_foreign_key": field.id in fk_field_ids,
                "references": references.get(field.id),
                "enum": enum_names.get(field.enum_id) if field.enum_id else None,
                "pii": pii,
                "sensitivity": sensitivity,
                "comment": field.comment,
                "default": field.default,
            })
        tables.append({
            "id": table.id,
            "name": table.name,
            "comment": table.comment,
            "kind": table.kind,
            "fields": fields,
        })

    indexes_by_table: dict[str, list[dict[str, Any]]] = {}
    field_name = {f.id: f.name for t in schema.logical.tables for f in t.fields}
    if schema.physical:
        for idx in schema.physical.indexes:
            cols = [field_name.get(c, c) for c in idx.columns]
            indexes_by_table.setdefault(idx.table_id, []).append(
                {"name": idx.id, "columns": cols, "unique": idx.unique, "type": idx.type}
            )

    enums = [
        {"name": e.name, "values": [v.value for v in e.values]}
        for e in schema.logical.enums
    ]
    meta = schema.meta
    return {
        "name": (meta.name if meta and meta.name else "schema"),
        "driver": (meta.default_driver if meta and meta.default_driver else "postgres"),
        "tables": tables,
        "indexes": indexes_by_table,
        "enums": enums,
    }


# --------------------------------------------------------------------------------------------------
# YAML — a clean, resolved schema (not the raw internal document) so it is readable and tool-friendly.
# --------------------------------------------------------------------------------------------------
def to_yaml(schema: SchemaJson) -> str:
    rm = _resolved(schema)
    doc: dict[str, Any] = {"database": rm["name"], "driver": rm["driver"], "tables": []}
    for t in rm["tables"]:
        columns = []
        for f in t["fields"]:
            col: dict[str, Any] = {"name": f["name"], "type": f["physical_label"], "nullable": f["nullable"]}
            if f["is_primary_key"]:
                col["primaryKey"] = True
            if f["references"]:
                col["references"] = f"{f['references'][0]}.{f['references'][1]}"
            if f["enum"]:
                col["enum"] = f["enum"]
            if f["pii"]:
                col["sensitive"] = f["sensitivity"] or True
            if f["comment"]:
                col["description"] = f["comment"]
            columns.append(col)
        entry: dict[str, Any] = {"name": t["name"]}
        if t["comment"]:
            entry["description"] = t["comment"]
        entry["columns"] = columns
        idx = rm["indexes"].get(t["id"])
        if idx:
            entry["indexes"] = [
                {"columns": i["columns"], "unique": i["unique"]} for i in idx
            ]
        doc["tables"].append(entry)
    if rm["enums"]:
        doc["enums"] = rm["enums"]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


# --------------------------------------------------------------------------------------------------
# DBML — dbdiagram.io. Tables, columns (resolved types), inline FK refs, indexes and enums.
# --------------------------------------------------------------------------------------------------
def _dbml_value(value: str) -> str:
    return f'"{value}"' if (not value or any(c in value for c in " -")) else value


def to_dbml(schema: SchemaJson) -> str:
    rm = _resolved(schema)
    lines: list[str] = []
    for t in rm["tables"]:
        lines.append(f"Table {t['name']} {{")
        for f in t["fields"]:
            col_type = f["enum"] or f["physical_label"]
            settings: list[str] = []
            if f["is_primary_key"]:
                settings.append("pk")
            if not f["nullable"] and not f["is_primary_key"]:
                settings.append("not null")
            if f["references"]:
                settings.append(f"ref: > {f['references'][0]}.{f['references'][1]}")
            note = f["comment"]
            if not note and f["pii"]:
                note = f"PII ({f['sensitivity']})" if f["sensitivity"] else "PII"
            if note:
                settings.append(f"note: '{str(note).replace(chr(39), '')}'")
            suffix = f" [{', '.join(settings)}]" if settings else ""
            lines.append(f"  {f['name']} {col_type}{suffix}")
        idx = rm["indexes"].get(t["id"])
        if idx:
            lines.append("  indexes {")
            for i in idx:
                cols = ", ".join(i["columns"])
                spec = f"({cols})" if len(i["columns"]) > 1 else cols
                lines.append(f"    {spec}{' [unique]' if i['unique'] else ''}")
            lines.append("  }")
        if t["comment"]:
            lines.append(f"  Note: '{t['comment'].replace(chr(39), '')}'")
        lines.append("}")
        lines.append("")
    for e in rm["enums"]:
        lines.append(f"Enum {e['name']} {{")
        for v in e["values"]:
            lines.append(f"  {_dbml_value(v)}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------------------------------
# JSON Schema (draft 2020-12) — each table an object schema; physical base type → JSON Schema type.
# --------------------------------------------------------------------------------------------------
def _json_schema_type(physical: dict[str, Any], enum_values: list[str] | None) -> dict[str, Any]:
    if enum_values:
        return {"type": "string", "enum": enum_values}
    base = str(physical.get("type", "")).lower()
    if base == "uuid":
        return {"type": "string", "format": "uuid"}
    if base in {"varchar", "char", "bpchar", "text", "citext"}:
        out: dict[str, Any] = {"type": "string"}
        if physical.get("length"):
            out["maxLength"] = physical["length"]
        return out
    if base in {"integer", "int", "int4", "bigint", "int8", "smallint", "int2", "serial", "bigserial"}:
        return {"type": "integer"}
    if base in {"numeric", "decimal", "money", "double precision", "real", "float", "double"}:
        return {"type": "number"}
    if base in {"boolean", "bool"}:
        return {"type": "boolean"}
    if base in {"timestamp", "timestamptz", "datetime"}:
        return {"type": "string", "format": "date-time"}
    if base == "date":
        return {"type": "string", "format": "date"}
    if base == "time":
        return {"type": "string", "format": "time"}
    if base in {"json", "jsonb"}:
        return {}  # any
    return {"type": "string"}  # safe default


def to_json_schema(schema: SchemaJson) -> str:
    rm = _resolved(schema)
    defs: dict[str, Any] = {}
    enum_by_name = {e["name"]: e["values"] for e in rm["enums"]}
    for t in rm["tables"]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for f in t["fields"]:
            prop = _json_schema_type(f["physical"], enum_by_name.get(f["enum"]) if f["enum"] else None)
            if f["comment"]:
                prop = {**prop, "description": f["comment"]}
            properties[f["name"]] = prop
            if not f["nullable"]:
                required.append(f["name"])
        table_schema: dict[str, Any] = {"type": "object", "title": t["name"], "properties": properties}
        if required:
            table_schema["required"] = required
        table_schema["additionalProperties"] = False
        defs[t["name"]] = table_schema
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": rm["name"],
        "$defs": defs,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------------------------------
# Data dictionary (Markdown) — the documentation artifact: every table/field with type, nullability,
# keys, relations, sensitive marks and descriptions.
# --------------------------------------------------------------------------------------------------
def _key_label(f: dict[str, Any]) -> str:
    parts = []
    if f["is_primary_key"]:
        parts.append("PK")
    if f["is_foreign_key"] and f["references"]:
        parts.append(f"FK → {f['references'][0]}.{f['references'][1]}")
    elif f["is_foreign_key"]:
        parts.append("FK")
    return ", ".join(parts)


def to_data_dictionary(schema: SchemaJson) -> str:
    rm = _resolved(schema)
    out: list[str] = [f"# Data Dictionary — {rm['name']}", ""]
    out.append(f"_{len(rm['tables'])} tables · driver `{rm['driver']}`_")
    out.append("")
    for t in rm["tables"]:
        out.append(f"## {t['name']}")
        if t["comment"]:
            out.append("")
            out.append(t["comment"])
        out.append("")
        out.append("| Column | Type | Null | Key | Sensitive | Description |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for f in t["fields"]:
            sensitive = f"PII ({f['sensitivity']})" if f["pii"] and f["sensitivity"] else ("PII" if f["pii"] else "")
            ftype = f["enum"] and f"enum({f['enum']})" or f["physical_label"]
            out.append(
                f"| {f['name']} | `{ftype}` | {'yes' if f['nullable'] else 'no'} "
                f"| {_key_label(f)} | {sensitive} | {f['comment'] or ''} |"
            )
        idx = rm["indexes"].get(t["id"])
        if idx:
            out.append("")
            for i in idx:
                kind = "unique index" if i["unique"] else "index"
                out.append(f"- {kind}: ({', '.join(i['columns'])})")
        out.append("")
    if rm["enums"]:
        out.append("## Enums")
        out.append("")
        for e in rm["enums"]:
            out.append(f"- **{e['name']}**: {', '.join(e['values'])}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------------------------------
# Dispatch.
# --------------------------------------------------------------------------------------------------
_EXPORTERS = {
    "yaml": to_yaml,
    "dbml": to_dbml,
    "jsonschema": to_json_schema,
    "datadict": to_data_dictionary,
}


def export(schema: SchemaJson, kind: str) -> tuple[str, str]:
    """Render ``kind`` from a resolved ``schema_json`` → ``(content, language)``. Raises on unknown."""
    fn = _EXPORTERS.get(kind)
    if fn is None:
        raise ValueError(f"unsupported export kind {kind!r}")
    return fn(schema), _LANGUAGE[kind]
