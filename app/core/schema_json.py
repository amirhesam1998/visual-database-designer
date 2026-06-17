"""The layered, versioned ``schema_json`` format — implementation of AD-3.

``schema_json`` is the backbone of the whole product: UI, CLI, importer, exporter, validator, the
diff/risk engines and the AI-SaaS pipeline all speak this one format. It is split into five
concern-isolated layers (``docs/spec-schema-json-format.md``):

* ``logical``      — DB/framework-agnostic tables, relations, enums
* ``physical``     — indexes, driver overrides (schema-affecting)
* ``semantic``     — ownership, tenancy, business rules, state machines
* ``presentation`` — canvas layout (NOT schema-affecting; diff ignores it)
* ``generation``   — namespaced adapter hints

Two deliberate boundaries:

1. **Structural vs. referential validation.** The bundled JSON Schema (``schema_json.schema.json``)
   only checks *structure*. Referential integrity (relation targets exist, exactly one initial
   state, …) is the Validation Engine's job (:mod:`app.core.validation`) — see spec §10.
2. **Format version vs. user schema version.** ``formatVersion`` is the semver of *this format*;
   ``meta.schemaVersion`` is the user's own label. They never mix (spec §9).

Pydantic models use camelCase aliases so they round-trip 1:1 with the JSON Schema while staying
snake_case in Python.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CURRENT_FORMAT_VERSION = "1.0.0"

_SCHEMA_FILE = Path(__file__).with_name("schema_json.schema.json")


class SchemaStructuralError(ValueError):
    """Raised when a payload fails *structural* (JSON-Schema) validation.

    ``errors`` is a list of human-readable ``"<json-path>: <message>"`` strings.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "structural validation failed")


@lru_cache(maxsize=1)
def _meta_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = _meta_schema()
    Draft202012Validator.check_schema(schema)  # the meta-schema itself must be valid (spec §11)
    return Draft202012Validator(schema)


def validate_structure(data: dict[str, Any]) -> list[str]:
    """Return a list of structural errors (empty == valid). Does NOT check referential integrity."""
    errors: list[str] = []
    for err in sorted(_validator().iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return errors


# --------------------------------------------------------------------------------------------------
# Pydantic models (camelCase aliases, snake_case in Python).
# --------------------------------------------------------------------------------------------------
class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Meta(_Camel):
    name: str | None = None
    description: str | None = None
    database_type: str | None = None
    default_driver: str | None = None
    schema_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ValidationOverride(_Camel):
    add: list[str] | None = None
    remove: list[str] | None = None


class PrivacyOverride(_Camel):
    pii: bool | None = None
    sensitivity: str | None = None  # none | low | medium | high


class FieldOverrides(_Camel):
    physical: dict[str, Any] | None = None
    validation: ValidationOverride | None = None
    form_input: dict[str, Any] | None = None
    privacy: PrivacyOverride | None = None


class Field_(_Camel):  # noqa: N801 - trailing underscore avoids clashing with pydantic's `Field`
    id: str
    name: str
    display_name: dict[str, str] | None = None
    comment: str | None = None
    semantic_type: str
    nullable: bool = False
    default: str | int | float | bool | None = None
    is_primary_key: bool = False
    auto_increment: bool = False
    enum_id: str | None = None
    overrides: FieldOverrides | None = None


class Table(_Camel):
    id: str
    name: str
    display_name: dict[str, str] | None = None
    comment: str | None = None
    domain: str | None = None
    kind: str | None = None  # normal | pivot | log | config | system
    fields: list[Field_] = Field(default_factory=list)

    def field_by_id(self, fid: str) -> Field_ | None:
        return next((f for f in self.fields if f.id == fid), None)

    def primary_keys(self) -> list[Field_]:
        return [f for f in self.fields if f.is_primary_key]


class Pivot(_Camel):
    table_id: str | None = None
    table_name: str | None = None


class Relation(_Camel):
    id: str
    name: str | None = None
    type: str  # one_to_one | one_to_many | many_to_many | polymorphic | self | has_many_through | embedded
    from_table_id: str
    to_table_id: str | None = None
    through_table_id: str | None = None
    pivot: Pivot | None = None
    foreign_key_field_id: str | None = None
    morph_name: str | None = None
    on_delete: str | None = None
    on_update: str | None = None


class EnumValue(_Camel):
    value: str
    label: dict[str, str] | None = None


class EnumDef(_Camel):
    id: str
    name: str
    values: list[EnumValue] = Field(default_factory=list)


class Logical(_Camel):
    tables: list[Table] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    enums: list[EnumDef] = Field(default_factory=list)


class Index(_Camel):
    id: str
    table_id: str
    columns: list[str] = Field(default_factory=list)
    unique: bool = False
    type: str | None = None


class Physical(_Camel):
    indexes: list[Index] = Field(default_factory=list)
    driver_overrides: dict[str, dict[str, Any]] | None = None


class Tenancy(_Camel):
    model: str | None = None  # single | row | schema_per_tenant | db_per_tenant
    tenant_key_field_by_table: dict[str, str] | None = None


class BusinessRule(_Camel):
    id: str
    category: str  # invariant | validation | service | workflow
    severity: str | None = None
    intent: str
    structured: dict[str, Any] | None = None
    targets: list[str] | None = None
    enabled: bool = True


class State(_Camel):
    id: str
    name: str
    label: dict[str, str] | None = None
    initial: bool = False
    final: bool = False
    color: str | None = None


class Transition(_Camel):
    id: str
    name: str | None = None
    from_: str = Field(alias="from")
    to: str
    guard: str | None = None
    permission: str | None = None
    side_effects: list[str] | None = None


class StateMachine(_Camel):
    id: str
    name: str
    field_id: str
    states: list[State] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)

    def state_by_id(self, sid: str) -> State | None:
        return next((s for s in self.states if s.id == sid), None)


class Semantic(_Camel):
    ownership: dict[str, str] | None = None
    tenancy: Tenancy | None = None
    business_rules: list[BusinessRule] = Field(default_factory=list)
    state_machines: list[StateMachine] = Field(default_factory=list)


class PresentationNode(_Camel):
    table_id: str
    x: float
    y: float
    collapsed: bool = False
    color: str | None = None
    group: str | None = None


class Viewport(_Camel):
    zoom: float | None = None
    offset_x: float | None = None
    offset_y: float | None = None


class Presentation(_Camel):
    nodes: list[PresentationNode] = Field(default_factory=list)
    viewport: Viewport | None = None


class SchemaJson(_Camel):
    """The root document. Only ``formatVersion`` and ``logical`` are required (per JSON Schema)."""

    format_version: str = CURRENT_FORMAT_VERSION
    meta: Meta | None = None
    logical: Logical = Field(default_factory=Logical)
    physical: Physical | None = None
    semantic: Semantic | None = None
    presentation: Presentation | None = None
    generation: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None

    # convenience lookups -------------------------------------------------------------------------
    def table_by_id(self, tid: str) -> Table | None:
        return next((t for t in self.logical.tables if t.id == tid), None)

    def all_fields(self) -> list[tuple[Table, Field_]]:
        return [(t, f) for t in self.logical.tables for f in t.fields]

    def field_by_id(self, fid: str) -> tuple[Table, Field_] | None:
        return next(((t, f) for t, f in self.all_fields() if f.id == fid), None)

    def enum_by_id(self, eid: str) -> EnumDef | None:
        return next((e for e in self.logical.enums if e.id == eid), None)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


# --------------------------------------------------------------------------------------------------
# Format migration (spec §9). Deterministic upgraders keyed by the *source* major version.
# --------------------------------------------------------------------------------------------------
_UPGRADERS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def _register_upgrader(from_major: int) -> Callable[..., Any]:
    def deco(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _UPGRADERS[from_major] = fn
        return fn

    return deco


@_register_upgrader(0)
def _upgrade_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Pre-1.0 documents kept ``tables``/``relations``/``enums`` at the root and had no layers.

    Wrap them into ``logical`` and stamp the current format version. Deterministic and lossless.
    """
    data = dict(data)
    logical = data.get("logical")
    if not isinstance(logical, dict):
        logical = {}
    for key in ("tables", "relations", "enums"):
        if key in data and key not in logical:
            logical[key] = data.pop(key)
    data["logical"] = logical
    data["formatVersion"] = "1.0.0"
    return data


def _major(version: str) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, IndexError):
        return 0


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an arbitrary ``schema_json`` payload up to :data:`CURRENT_FORMAT_VERSION`.

    Applies the registered major-version upgraders in sequence. A document with no
    ``formatVersion`` but a top-level ``tables`` array is treated as pre-1.0 (major 0).
    """
    data = dict(data)
    if "formatVersion" not in data:
        data["formatVersion"] = "0.0.0" if "tables" in data else CURRENT_FORMAT_VERSION
    target = _major(CURRENT_FORMAT_VERSION)
    guard = 0
    while _major(data["formatVersion"]) < target:
        guard += 1
        if guard > 16:  # pragma: no cover - defensive against a broken upgrader chain
            raise RuntimeError("format migration did not converge")
        major = _major(data["formatVersion"])
        upgrader = _UPGRADERS.get(major)
        if upgrader is None:
            raise SchemaStructuralError([f"no format upgrader registered for major version {major}"])
        data = upgrader(data)
    return data


def load(data: dict[str, Any], *, validate: bool = True) -> SchemaJson:
    """Migrate → (optionally) structurally validate → parse into a :class:`SchemaJson`.

    Raises :class:`SchemaStructuralError` if ``validate`` and the (migrated) payload is
    structurally invalid. Referential integrity is intentionally NOT checked here (use the
    Validation Engine).
    """
    migrated = migrate(data)
    if validate:
        errors = validate_structure(migrated)
        if errors:
            raise SchemaStructuralError(errors)
    return SchemaJson.model_validate(migrated)


def dump(schema: SchemaJson) -> dict[str, Any]:
    """Serialize a :class:`SchemaJson` back to a plain JSON-able dict (camelCase keys)."""
    return schema.to_dict()
