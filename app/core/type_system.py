"""Two-layer Type System (Semantic ↔ Physical) — implementation of AD-2.

A field stores only a ``semanticType`` (+ optional ``overrides``). One registry record then
*deterministically* resolves everything the six downstream consumers need, so the "this is an
email → unique + format + lowercase" knowledge lives in exactly one place instead of being
re-derived in validation, forms, admin, API, seeding and privacy. See ``docs/spec-type-system.md``.

Resolution pipeline (spec §4)::

    field.semanticType
       → registry.lookup(semanticType)
       → apply driver-specific physical
       → merge field.overrides
       → emit to the target consumer

Nothing here calls an LLM; reverse inference is a deterministic heuristic that a human approves
(AD-5).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.schema_json import Field_, SchemaJson

# Default driver used when a caller does not specify one.
DEFAULT_DRIVER = "postgres"


class UnsupportedPhysicalTypeError(ValueError):
    """A semantic type has no physical representation on a given driver (spec §8 — no silent coercion)."""


class PhysicalSpec(BaseModel):
    type: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    dimension: int | None = None  # vector(N)

    def merged(self, override: dict[str, Any] | None) -> dict[str, Any]:
        base = self.model_dump(exclude_none=True)
        if override:
            base.update({k: v for k, v in override.items() if v is not None})
        return base


class SemanticTypeDef(BaseModel):
    """One record in the Type Registry — the single source of truth for a semantic type."""

    id: str
    label: dict[str, str] = Field(default_factory=dict)
    category: str = "string"
    physical_default: PhysicalSpec
    physical_by_driver: dict[str, PhysicalSpec] = Field(default_factory=dict)
    unsupported_on: list[str] = Field(default_factory=list)
    validation_rules: list[str] = Field(default_factory=list)
    validation_overridable: bool = True
    form_component: str = "text-input"
    form_props: dict[str, Any] = Field(default_factory=dict)
    pii: bool = False
    sensitivity: str = "none"  # none | low | medium | high
    fake_generator: str = "word"
    fake_params: dict[str, Any] = Field(default_factory=dict)
    api_masking: dict[str, Any] | None = None
    openapi: dict[str, Any] = Field(default_factory=lambda: {"type": "string"})

    def physical_for(self, driver: str) -> PhysicalSpec:
        if driver in self.unsupported_on:
            raise UnsupportedPhysicalTypeError(
                f"semantic type {self.id!r} has no physical type on driver {driver!r}"
            )
        return self.physical_by_driver.get(driver, self.physical_default)


class ResolvedField(BaseModel):
    """The fully-resolved view of a field, ready to hand to any single consumer."""

    name: str
    semantic_type: str
    nullable: bool
    driver: str
    physical: dict[str, Any]
    validation: list[str]
    form: dict[str, Any]
    openapi: dict[str, Any]
    fake: dict[str, Any]
    privacy: dict[str, Any]
    api_masking: dict[str, Any] | None

    def consumers(self) -> dict[str, Any]:
        """All six consumer projections in one dict — used for snapshot conformance tests (spec §9)."""
        return {
            "migration": self.physical,
            "validation": self.validation,
            "form": self.form,
            "admin": {"form": self.form, "masking": self.api_masking},
            "openapi": self.openapi,
            "seeder": self.fake,
            "gdpr": self.privacy,
            "api": self.api_masking,
        }


# --------------------------------------------------------------------------------------------------
# The Type Registry.
# --------------------------------------------------------------------------------------------------
def _norm(type_id: str) -> str:
    """Normalise a semantic type id: drop the ``core:`` namespace so 'email' == 'core:email'."""
    tid = (type_id or "").strip()
    if tid.startswith("core:"):
        return tid[len("core:") :]
    return tid


class TypeRegistry:
    def __init__(self) -> None:
        self._types: dict[str, SemanticTypeDef] = {}

    def register(self, definition: SemanticTypeDef, *, replace: bool = False) -> None:
        key = _norm(definition.id)
        if key in self._types and not replace:
            raise ValueError(f"semantic type {definition.id!r} already registered (namespace collision)")
        self._types[key] = definition

    def get(self, type_id: str) -> SemanticTypeDef:
        key = _norm(type_id)
        if key not in self._types:
            raise KeyError(f"unknown semantic type {type_id!r}")
        return self._types[key]

    def has(self, type_id: str) -> bool:
        return _norm(type_id) in self._types

    def ids(self) -> list[str]:
        return sorted(self._types)

    # -- the resolution pipeline (spec §4) --------------------------------------------------------
    def resolve(self, field: Field_, driver: str = DEFAULT_DRIVER) -> ResolvedField:
        definition = self.get(field.semantic_type)
        ov = field.overrides

        physical = definition.physical_for(driver).merged(ov.physical if ov else None)

        validation = list(definition.validation_rules)
        if ov and ov.validation and definition.validation_overridable:
            for rule in ov.validation.remove or []:
                if rule in validation:
                    validation.remove(rule)
            for rule in ov.validation.add or []:
                if rule not in validation:
                    validation.append(rule)
        if not field.nullable and "required" not in validation:
            validation = ["required", *validation]

        form = {"component": definition.form_component, "props": dict(definition.form_props)}
        if ov and ov.form_input:
            form["component"] = ov.form_input.get("component", form["component"])
            form["props"].update(ov.form_input.get("props", {}))

        privacy = {"pii": definition.pii, "sensitivity": definition.sensitivity}
        if ov and ov.privacy:
            if ov.privacy.pii is not None:
                privacy["pii"] = ov.privacy.pii
            if ov.privacy.sensitivity is not None:
                privacy["sensitivity"] = ov.privacy.sensitivity

        return ResolvedField(
            name=field.name,
            semantic_type=_norm(field.semantic_type),
            nullable=field.nullable,
            driver=driver,
            physical=physical,
            validation=validation,
            form=form,
            openapi=dict(definition.openapi),
            fake={"generator": definition.fake_generator, "params": dict(definition.fake_params)},
            privacy=privacy,
            api_masking=definition.api_masking,
        )


# --------------------------------------------------------------------------------------------------
# Reverse inference (spec §8 / §2 import path): physical + name → suggested semantic type.
# --------------------------------------------------------------------------------------------------
class InferenceResult(BaseModel):
    semantic_type: str
    confidence: float  # 0..1


_MONEY_NAMES = {"price", "total", "amount", "cost", "balance", "fee", "salary", "subtotal"}


def infer_semantic_type(physical_type: str, name: str = "", *, unique: bool = False) -> InferenceResult:
    """Best-effort, deterministic guess of a semantic type from a physical column (for import)."""
    pt = (physical_type or "").strip().lower()
    n = (name or "").strip().lower()

    if "vector" in pt or "embedding" in n:
        return InferenceResult(semantic_type="vector_embedding", confidence=0.95)
    if pt in {"uuid", "uniqueidentifier"}:
        return InferenceResult(semantic_type="uuid", confidence=0.9)
    if pt in {"bool", "boolean", "tinyint"}:
        return InferenceResult(semantic_type="boolean", confidence=0.9)
    if pt in {"timestamp", "timestamptz", "datetime"}:
        return InferenceResult(semantic_type="datetime", confidence=0.85)
    if pt == "date":
        return InferenceResult(semantic_type="date", confidence=0.9)
    if pt in {"decimal", "numeric", "money", "float", "double"}:
        if any(k in n for k in _MONEY_NAMES):
            return InferenceResult(semantic_type="money", confidence=0.8)
        return InferenceResult(semantic_type="decimal", confidence=0.7)
    if pt in {"int", "integer", "int4", "serial", "smallint"}:
        return InferenceResult(semantic_type="integer", confidence=0.8)
    if pt in {"bigint", "int8", "bigserial"}:
        return InferenceResult(semantic_type="big_integer", confidence=0.8)
    if pt in {"text", "longtext", "mediumtext"}:
        return InferenceResult(semantic_type="text", confidence=0.7)

    # varchar / char family — lean on the name.
    if "email" in n:
        return InferenceResult(semantic_type="email", confidence=0.95 if unique else 0.85)
    if "password" in n or n in {"pwd", "pass"}:
        return InferenceResult(semantic_type="password", confidence=0.95)
    if "slug" in n:
        return InferenceResult(semantic_type="slug", confidence=0.85)
    if "url" in n or "link" in n:
        return InferenceResult(semantic_type="url", confidence=0.8)
    if "phone" in n or "mobile" in n:
        return InferenceResult(semantic_type="phone_ir", confidence=0.75)
    if n.endswith("_id") or n == "id":
        return InferenceResult(semantic_type="foreign_key" if n != "id" else "uuid", confidence=0.6)
    return InferenceResult(semantic_type="string", confidence=0.5)


# --------------------------------------------------------------------------------------------------
# Built-in registry (spec §5). Concise helpers keep the table readable.
# --------------------------------------------------------------------------------------------------
def _t(
    id: str,
    category: str,
    physical: dict[str, Any],
    *,
    by_driver: dict[str, dict[str, Any]] | None = None,
    unsupported_on: list[str] | None = None,
    rules: list[str] | None = None,
    component: str = "text-input",
    props: dict[str, Any] | None = None,
    pii: bool = False,
    sensitivity: str = "none",
    fake: str = "word",
    fake_params: dict[str, Any] | None = None,
    masking: dict[str, Any] | None = None,
    openapi: dict[str, Any] | None = None,
) -> SemanticTypeDef:
    return SemanticTypeDef(
        id=id,
        category=category,
        physical_default=PhysicalSpec(**physical),
        physical_by_driver={d: PhysicalSpec(**p) for d, p in (by_driver or {}).items()},
        unsupported_on=unsupported_on or [],
        validation_rules=rules or [],
        form_component=component,
        form_props=props or {},
        pii=pii,
        sensitivity=sensitivity,
        fake_generator=fake,
        fake_params=fake_params or {},
        api_masking=masking,
        openapi=openapi or {"type": "string"},
    )


def build_default_registry() -> TypeRegistry:
    reg = TypeRegistry()
    defs: list[SemanticTypeDef] = [
        # --- string ---
        _t("string", "string", {"type": "varchar", "length": 255}, rules=["string", "max:255"]),
        _t("text", "string", {"type": "text"}, rules=["string"], component="textarea", openapi={"type": "string"}),
        _t(
            "email", "string", {"type": "varchar", "length": 255},
            rules=["email", "max:255"], component="email-input", pii=True, sensitivity="medium",
            fake="email", masking={"strategy": "partial", "show": "domain"},
            openapi={"type": "string", "format": "email"},
        ),
        _t("url", "string", {"type": "varchar", "length": 2048}, rules=["url"], component="url-input",
           openapi={"type": "string", "format": "uri"}),
        _t("slug", "string", {"type": "varchar", "length": 255}, rules=["slug", "max:255"], fake="slug"),
        _t(
            "password", "string", {"type": "varchar", "length": 255}, rules=["min:8"],
            component="password-input", pii=True, sensitivity="high", fake="password",
            masking={"strategy": "hidden"}, openapi={"type": "string", "format": "password", "writeOnly": True},
        ),
        _t(
            "phone_ir", "string", {"type": "varchar", "length": 15}, rules=["regex:^09\\d{9}$"],
            component="masked-text", props={"mask": "09#########"}, pii=True, sensitivity="medium",
            fake="phone_ir", masking={"strategy": "partial", "show": "last4"},
            openapi={"type": "string", "pattern": "^09[0-9]{9}$"},
        ),
        _t(
            "national_code", "string", {"type": "varchar", "length": 10},
            rules=["digits:10", "iran_national_code"], component="masked-text", props={"mask": "##########"},
            pii=True, sensitivity="high", fake="national_code_ir",
            masking={"strategy": "partial", "show": "last4"},
            openapi={"type": "string", "pattern": "^[0-9]{10}$"},
        ),
        _t("uuid", "string", {"type": "uuid"}, rules=["uuid"],
           openapi={"type": "string", "format": "uuid"}),
        _t("color", "string", {"type": "varchar", "length": 9}, rules=["regex:^#"], component="color-picker"),
        _t("markdown", "string", {"type": "text"}, component="markdown-editor"),
        # --- numeric ---
        _t("integer", "numeric", {"type": "integer"}, rules=["integer"], component="number-input", fake="number",
           openapi={"type": "integer", "format": "int32"}),
        _t("big_integer", "numeric", {"type": "bigint"}, rules=["integer"], component="number-input", fake="number",
           openapi={"type": "integer", "format": "int64"}),
        _t("decimal", "numeric", {"type": "decimal", "precision": 12, "scale": 2}, rules=["numeric"],
           component="number-input", fake="decimal", openapi={"type": "number", "format": "double"}),
        _t(
            "money", "numeric", {"type": "decimal", "precision": 12, "scale": 2},
            by_driver={"postgres": {"type": "numeric", "precision": 12, "scale": 2}},
            rules=["numeric", "min:0"], component="currency-input", props={"step": "0.01"},
            fake="decimal_in_range", fake_params={"min": 0, "max": 100000, "scale": 2},
            openapi={"type": "number", "format": "decimal"},
        ),
        _t("percentage", "numeric", {"type": "decimal", "precision": 5, "scale": 2},
           rules=["numeric", "min:0", "max:100"], component="number-input", openapi={"type": "number"}),
        _t("rating", "numeric", {"type": "integer"}, rules=["integer", "min:0", "max:5"], component="rating-input",
           openapi={"type": "integer"}),
        # --- temporal ---
        _t("date", "temporal", {"type": "date"}, rules=["date"], component="date-picker", fake="date",
           openapi={"type": "string", "format": "date"}),
        _t("datetime", "temporal", {"type": "timestamp"}, rules=["date"], component="datetime-picker", fake="datetime",
           openapi={"type": "string", "format": "date-time"}),
        _t("timestamp", "temporal", {"type": "timestamp"}, rules=["date"], component="datetime-picker", fake="datetime",
           openapi={"type": "string", "format": "date-time"}),
        _t("time", "temporal", {"type": "time"}, component="time-picker", openapi={"type": "string", "format": "time"}),
        _t("duration", "temporal", {"type": "integer"}, rules=["integer", "min:0"], component="number-input",
           openapi={"type": "integer"}),
        # --- logical / choice ---
        _t("boolean", "boolean", {"type": "boolean"}, rules=["boolean"], component="switch", fake="boolean",
           openapi={"type": "boolean"}),
        _t("enum", "choice", {"type": "varchar", "length": 64}, rules=["in_enum"], component="select", fake="enum"),
        _t("status", "choice", {"type": "varchar", "length": 64}, rules=["in_enum"], component="select", fake="enum"),
        # --- structural ---
        _t("json", "structural", {"type": "jsonb"},
           by_driver={"mysql": {"type": "json"}}, component="key-value-editor", openapi={"type": "object"}),
        _t("array", "structural", {"type": "jsonb"},
           by_driver={"mysql": {"type": "json"}}, component="tag-input", openapi={"type": "array"}),
        _t("foreign_key", "structural", {"type": "bigint"}, rules=["integer", "exists"], component="searchable-select",
           fake="foreign_ref", openapi={"type": "integer", "format": "int64"}),
        _t("morph_to", "structural", {"type": "varchar", "length": 255}, component="morph-select"),
        # --- file / media ---
        _t("image", "media", {"type": "varchar", "length": 2048}, component="image-uploader",
           openapi={"type": "string", "format": "uri"}),
        _t("file", "media", {"type": "varchar", "length": 2048}, component="file-uploader",
           openapi={"type": "string", "format": "uri"}),
        _t("avatar", "media", {"type": "varchar", "length": 2048}, component="image-uploader", pii=True,
           sensitivity="low", openapi={"type": "string", "format": "uri"}),
        # --- AI ---
        _t(
            "vector_embedding", "ai", {"type": "vector", "dimension": 1536},
            by_driver={"postgres": {"type": "vector", "dimension": 1536}},
            unsupported_on=["mysql", "sqlite"], component="hidden", fake="vector",
            openapi={"type": "array", "items": {"type": "number"}},
        ),
    ]
    for d in defs:
        reg.register(d)
    return reg


# Module-level singleton — cheap to build, safe to share (immutable definitions).
DEFAULT_REGISTRY = build_default_registry()


# --------------------------------------------------------------------------------------------------
# Foreign-key physical resolution (shared resolution step — spec M2 §7).
# --------------------------------------------------------------------------------------------------
def resolve_fk_physical(
    schema: SchemaJson, driver: str = DEFAULT_DRIVER, reg: TypeRegistry | None = None
) -> dict[str, dict[str, Any]]:
    """Map each foreign-key field id → the resolved *physical* spec of the primary key it references.

    A foreign-key column's storage type is dictated by the referenced primary key (Postgres rejects
    the constraint otherwise — the bug Milestone 1's live test caught). That resolution belongs to
    the Type System pipeline, not any single consumer: the SQL **emitter** uses it forward (to render
    an FK column with the PK's type), the **importer** validates the same invariant in reverse (it
    reads the real types), and future phase-2 generators (a seeder that must fabricate a uuid, not an
    integer, for an FK) all need the *same* resolved type from one place.

    Returns ``{field_id: physical_dict}`` only for FK fields whose referenced PK resolves cleanly;
    callers fall back to the field's own semantic type for anything not present.
    """
    registry = reg or DEFAULT_REGISTRY
    overrides: dict[str, dict[str, Any]] = {}
    for rel in schema.logical.relations:
        if not (rel.from_table_id and rel.to_table_id and rel.foreign_key_field_id):
            continue
        to_table = schema.table_by_id(rel.to_table_id)
        if to_table is None:
            continue
        pks = to_table.primary_keys()
        if not pks:
            continue
        try:
            overrides[rel.foreign_key_field_id] = registry.resolve(pks[0], driver).physical
        except (KeyError, UnsupportedPhysicalTypeError):
            continue  # referenced PK has no physical type on this driver — leave the FK column as-is
    return overrides
