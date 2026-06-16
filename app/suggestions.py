"""AI schema suggestions (task 6F.5).

`SchemaSuggestions.suggest_schema` turns a free-text request into a `DatabaseSchema`. With an LLM
it asks the model and validates/normalizes the result; with no LLM (or on failure) it falls back to
a deterministic, domain-aware heuristic so the module always produces a useful schema offline and
tests stay deterministic.

`suggest_improvements` returns advisory strings (indexes, constraints, performance, security).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from app.prompts import (
    DESIGN_SYSTEM_PROMPT,
    DESIGN_USER_PROMPT,
    IMPROVE_SYSTEM_PROMPT,
    IMPROVE_USER_PROMPT,
)
from app.schema_model import DatabaseSchema, FieldType, Relation, RelationType, SchemaField, Table
from app.templates import build_template_schema

if TYPE_CHECKING:
    from aiarch_module_sdk import LLMClient


class SchemaSuggestions:
    def __init__(self, llm_client: LLMClient | None):
        self.llm = llm_client

    # -- design -------------------------------------------------------------

    async def suggest_schema(self, feature_request: str, *, driver: str = "postgresql", ctx=None) -> DatabaseSchema:
        request = (feature_request or "").strip()
        fallback = build_template_schema(request, driver=driver)
        if self.llm is None or not request:
            return fallback
        try:
            data = await self.llm.complete(
                system=DESIGN_SYSTEM_PROMPT,
                user=DESIGN_USER_PROMPT.format(feature_request=request, driver=driver),
            )
            schema = build_schema_from_json(data, driver=driver)
            if schema.tables:
                schema.metadata["source"] = "llm"
                return schema
        except Exception as exc:  # noqa: BLE001 — degrade to the heuristic template
            if ctx is not None:
                ctx.log(f"llm schema design failed, using heuristic: {exc}")
        return fallback

    # -- improvements -------------------------------------------------------

    async def suggest_improvements(self, schema: DatabaseSchema, *, ctx=None) -> list[str]:
        suggestions = heuristic_improvements(schema)
        if self.llm is None:
            return suggestions
        try:
            data = await self.llm.complete(
                system=IMPROVE_SYSTEM_PROMPT,
                user=IMPROVE_USER_PROMPT.format(schema_json=json.dumps(schema.model_dump(mode="json"))),
            )
            llm_suggestions = data.get("suggestions", []) if isinstance(data, dict) else []
            for s in llm_suggestions:
                if isinstance(s, str) and s.strip() and s not in suggestions:
                    suggestions.append(s.strip())
        except Exception as exc:  # noqa: BLE001 — keep the heuristic suggestions
            if ctx is not None:
                ctx.log(f"llm improvement suggestions skipped: {exc}")
        return suggestions


# ---------------------------------------------------------------------------
# JSON → DatabaseSchema
# ---------------------------------------------------------------------------


def build_schema_from_json(data: dict, *, driver: str = "postgresql") -> DatabaseSchema:
    """Build a DatabaseSchema from loose LLM/JSON output, tolerating missing keys."""
    if not isinstance(data, dict):
        return DatabaseSchema(driver=driver)

    tables: list[Table] = []
    for raw_table in data.get("tables", []) or []:
        if not isinstance(raw_table, dict) or not raw_table.get("name"):
            continue
        fields = [_build_field(f) for f in raw_table.get("fields", []) or [] if isinstance(f, dict) and f.get("name")]
        if not any(f.primary_key for f in fields):
            fields.insert(0, SchemaField(name="id", type=FieldType.BIGINT, primary_key=True,
                                         auto_increment=True, nullable=False))
        tables.append(
            Table(
                name=str(raw_table["name"]),
                fields=fields,
                description=raw_table.get("description"),
                timestamps=bool(raw_table.get("timestamps", False)),
            )
        )

    schema = DatabaseSchema(id="ai-generated", type=str(data.get("type", "sql")), driver=driver, tables=tables)
    _attach_relations(schema, data.get("relations", []) or [])
    # Relations can also be nested under each table.
    for raw_table in data.get("tables", []) or []:
        if isinstance(raw_table, dict):
            _attach_relations(schema, raw_table.get("relations", []) or [], default_from=raw_table.get("name"))
    return schema


def _build_field(raw: dict) -> SchemaField:
    return SchemaField(
        name=str(raw.get("name")),
        type=FieldType.coerce(raw.get("type", "varchar")),
        nullable=bool(raw.get("nullable", True)),
        unique=bool(raw.get("unique", False)),
        indexed=bool(raw.get("indexed", False)),
        primary_key=bool(raw.get("primary_key", False)),
        auto_increment=bool(raw.get("auto_increment", raw.get("primary_key", False))),
        default=raw.get("default"),
        length=raw.get("length"),
        precision=raw.get("precision"),
        scale=raw.get("scale"),
        values=raw.get("values"),
        description=raw.get("description"),
    )


def _attach_relations(schema: DatabaseSchema, raw_relations: list, *, default_from: str | None = None) -> None:
    for raw in raw_relations:
        if not isinstance(raw, dict):
            continue
        from_table = raw.get("from_table") or default_from
        to_table = raw.get("to_table")
        if not from_table or not to_table:
            continue
        owner = schema.table(from_table)
        if owner is None:
            continue
        owner.relations.append(
            Relation(
                name=raw.get("name", ""),
                from_table=from_table,
                from_field=raw.get("from_field", ""),
                to_table=to_table,
                to_field=raw.get("to_field", "id"),
                type=_coerce_relation_type(raw.get("type")),
                on_delete=raw.get("on_delete", "cascade"),
                on_update=raw.get("on_update", "cascade"),
            )
        )


def _coerce_relation_type(value) -> RelationType:
    text = str(value or "").strip().lower().replace("-", "_")
    try:
        return RelationType(text)
    except ValueError:
        return RelationType.ONE_TO_MANY


# ---------------------------------------------------------------------------
# Heuristic improvement suggestions (always available)
# ---------------------------------------------------------------------------

_SECRET_FIELD_RE = re.compile(r"(?i)(password|secret|token|api_key|credit_card|ssn)$")


def heuristic_improvements(schema: DatabaseSchema) -> list[str]:
    out: list[str] = []
    for table in schema.tables:
        for field in table.fields:
            if field.name.endswith("_id") and not field.indexed and not field.primary_key:
                out.append(f"Add an index on {table.name}.{field.name} (foreign key) to speed up joins.")
            if _SECRET_FIELD_RE.search(field.name) and "password" in field.name.lower():
                out.append(f"Store {table.name}.{field.name} as a one-way hash (bcrypt/argon2), never in plaintext.")
            if field.name == "email" and not field.unique:
                out.append(f"Consider a unique constraint on {table.name}.email.")
        if not table.relations and table.name.endswith("s") and len(schema.tables) > 1:
            out.append(f"Table '{table.name}' has no relationships — confirm it is meant to stand alone.")
    return out
