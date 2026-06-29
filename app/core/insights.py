"""Insights Engine — deterministic design assistant (intelligence milestone §0–§3).

This turns the designer from a "schema painter" into a "design assistant": it looks at a
``schema_json`` and offers index suggestions, design warnings and sensitive-field detection. Unlike
the Validation Engine (which gates correctness), this is *advisory* — nothing is auto-applied; every
finding is something a human accepts or rejects (spec §5).

The one rule that organises everything is the **fact vs. suggestion** split (spec §0):

* a **fact** (``kind="fact"``) is certain from the structure — "this table has no primary key", "this
  foreign key stores a different type than the primary key it references". Reported with a clear
  ``severity`` (error/warning/info).
* a **suggestion** (``kind="suggestion"``) is a heuristic guess — "this column *looks* like a national
  code, mark it sensitive?". Carried as a suggestion the user confirms; never stated as fact.

Every insight has a stable ``rule_id``, a ``severity``, a human ``title`` and a **why** (the spec's
hard requirement — no black boxes). Some carry a structured ``action`` (add an index, mark a field
sensitive) that the front-end applies *through the normal edit + validate path* — the intelligence
never bypasses the engine's determinism or the approval gate (spec §5/§6).

Deterministic and LLM-free: the same schema always yields the same insights (spec §6). An LLM could
later enrich the *prose*, but correctness stays in this deterministic layer (spec §7).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.schema_json import SchemaJson
from app.core.type_system import (
    DEFAULT_REGISTRY,
    TypeRegistry,
    UnsupportedPhysicalTypeError,
)


class InsightKind(StrEnum):
    """The spec's central axis (§0): a certain structural fact vs. a heuristic guess."""

    FACT = "fact"
    SUGGESTION = "suggestion"


class InsightSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class InsightCategory(StrEnum):
    INDEX = "index"  # §1 Index Advisor
    DESIGN = "design"  # §2 Design Warnings
    PRIVACY = "privacy"  # §3 Sensitive Field Detection


class InsightAction(BaseModel):
    """A structured, engine-applicable action for a finding (spec §5).

    The front-end never decides *how* to apply it: it maps ``type`` onto an existing structural edit
    (``add_index`` → ``physical.indexes``; ``mark_sensitive`` → a field privacy override) and then the
    normal render + validate round-trip runs. The intelligence proposes; the engine validates.
    """

    type: str  # add_index | mark_sensitive
    label: str
    table_id: str | None = None
    field_id: str | None = None
    columns: list[str] | None = None
    unique: bool | None = None
    sensitivity: str | None = None


class Insight(BaseModel):
    rule_id: str
    category: InsightCategory
    kind: InsightKind
    severity: InsightSeverity
    title: str
    why: str  # the mandatory explanation (spec §0/§6) — never a black box
    path: str = ""  # human-readable location, e.g. "orders.user_id"
    entity_id: str | None = None  # the table/field/relation id the badge attaches to
    table_id: str | None = None
    field_id: str | None = None
    fix: str | None = None
    action: InsightAction | None = None


class InsightReport(BaseModel):
    insights: list[Insight] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


def _i(
    rule_id: str,
    category: InsightCategory,
    kind: InsightKind,
    severity: InsightSeverity,
    title: str,
    why: str,
    *,
    path: str = "",
    entity_id: str | None = None,
    table_id: str | None = None,
    field_id: str | None = None,
    fix: str | None = None,
    action: InsightAction | None = None,
) -> Insight:
    return Insight(
        rule_id=rule_id, category=category, kind=kind, severity=severity, title=title, why=why,
        path=path, entity_id=entity_id, table_id=table_id, field_id=field_id, fix=fix, action=action,
    )


# Tables that legitimately differ from the "normal" rules (a pivot has no PK of its own; a log table
# is intentionally an island). Mirrors the Validation Engine's carve-outs so the two never disagree.
_RELAXED_KINDS = {"pivot", "log"}

# §1 — column names that are very commonly looked up / filtered. The split decides the suggested
# index shape: identity-ish columns want a UNIQUE index; categorical columns want a plain one.
_UNIQUE_LOOKUP_NAMES = {"email", "username", "slug", "sku", "code", "handle"}
_FILTER_LOOKUP_NAMES = {"status", "state", "type", "kind", "category", "is_active", "active", "published"}

# §3 — name tokens that hint at sensitive data on an otherwise-generic column.
_SENSITIVE_TOKENS = {
    "ssn", "passport", "cvv", "cvc", "iban", "sheba", "salary", "income",
    "phone", "mobile", "tel", "address", "birth", "dob", "postal", "zip", "zipcode",
    "nationalcode", "melli", "creditcard", "cardnumber", "fin", "biometric",
}
_SENSITIVE_PHRASES = (
    ("national", "code"), ("national", "id"), ("credit", "card"), ("card", "number"),
    ("social", "security"), ("date", "of", "birth"), ("zip", "code"), ("postal", "code"),
    ("home", "address"), ("billing", "address"), ("shipping", "address"),
)
# Generic semantic types that carry no privacy signal of their own (a suspicious *name* on one of
# these is the §3 heuristic). FK/PK columns are excluded separately.
_GENERIC_TYPES = {"string", "text", "varchar", "char"}

_VARCHARISH = ("varchar", "char", "character")


def _tokens(name: str) -> list[str]:
    """Lowercase word tokens of an identifier, splitting on separators AND camelCase humps."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name or "")
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _looks_sensitive(name: str) -> bool:
    toks = _tokens(name)
    tokset = set(toks)
    if tokset & _SENSITIVE_TOKENS:
        return True
    return any(all(w in tokset for w in phrase) for phrase in _SENSITIVE_PHRASES)


def _physical_type(physical: dict) -> str:
    return str(physical.get("type", "")).lower()


def _physical_label(physical: dict) -> str:
    base = physical.get("type", "")
    if physical.get("length"):
        return f"{base}({physical['length']})"
    if physical.get("precision") is not None:
        return f"{base}({physical['precision']},{physical.get('scale', 0)})"
    return str(base)


# --------------------------------------------------------------------------------------------------
# §1 — Index Advisor.
# --------------------------------------------------------------------------------------------------
def _index_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Insight]:
    indexes = list(s.physical.indexes) if s.physical else []
    # A column is "covered" for lookups when it is the LEADING column of some index (a composite
    # index on (a, b) accelerates lookups on a, not on b alone). Primary keys are implicitly indexed.
    leading = {idx.columns[0] for idx in indexes if idx.columns}
    pk_field_ids = {f.id for t in s.logical.tables for f in t.primary_keys()}
    covered = leading | pk_field_ids

    fk_field_ids = {rel.foreign_key_field_id for rel in s.logical.relations if rel.foreign_key_field_id}
    field_table = {f.id: t for t in s.logical.tables for f in t.fields}

    # FK without a supporting index — almost always worth indexing (spec §1: "this is almost always
    # correct"). Carried as a suggestion (you *might* skip it deliberately) with an apply action.
    for rel in s.logical.relations:
        fid = rel.foreign_key_field_id
        if not fid or fid in covered:
            continue
        table = field_table.get(fid)
        if table is None:
            continue
        field = table.field_by_id(fid)
        loc = f"{table.name}.{field.name}" if field else table.name
        yield _i(
            "IDX001", InsightCategory.INDEX, InsightKind.SUGGESTION, InsightSeverity.WARNING,
            f"Index the foreign key {loc}",
            "Foreign keys drive joins and lookups; without an index the database falls back to a "
            "sequential scan on every join and every ON DELETE/UPDATE check.",
            path=loc, entity_id=fid, table_id=table.id, field_id=fid,
            fix=f"Add an index on {loc}.",
            action=InsightAction(type="add_index", label="Add index", table_id=table.id,
                                 columns=[fid], unique=False),
        )

    # Frequently-searched columns that aren't covered — a softer, name-based guess (spec §1).
    for table in s.logical.tables:
        for field in table.fields:
            if field.id in covered or field.id in fk_field_ids or field.is_primary_key:
                continue
            nm = field.name.lower()
            loc = f"{table.name}.{field.name}"
            if nm in _UNIQUE_LOOKUP_NAMES:
                yield _i(
                    "IDX002", InsightCategory.INDEX, InsightKind.SUGGESTION, InsightSeverity.INFO,
                    f"Add a unique index on {loc}",
                    f"Columns named {field.name!r} are typically looked up directly and expected to be "
                    "unique; a unique index both speeds the lookup and enforces no duplicates.",
                    path=loc, entity_id=field.id, table_id=table.id, field_id=field.id,
                    fix=f"Add a unique index on {loc}.",
                    action=InsightAction(type="add_index", label="Add unique index", table_id=table.id,
                                         columns=[field.id], unique=True),
                )
            elif nm in _FILTER_LOOKUP_NAMES:
                yield _i(
                    "IDX003", InsightCategory.INDEX, InsightKind.SUGGESTION, InsightSeverity.INFO,
                    f"Consider indexing {loc}",
                    f"Columns named {field.name!r} are commonly used to filter rows; an index helps when "
                    "the table grows (though low-cardinality columns benefit less).",
                    path=loc, entity_id=field.id, table_id=table.id, field_id=field.id,
                    fix=f"Add an index on {loc}.",
                    action=InsightAction(type="add_index", label="Add index", table_id=table.id,
                                         columns=[field.id], unique=False),
                )

    # Redundant indexes — two indexes covering the exact same columns (spec §1: "warn to remove").
    # Certain from the structure, so a fact; removal is left to the user (no auto-drop action).
    seen: dict[tuple[str, tuple[str, ...]], str] = {}
    for idx in indexes:
        key = (idx.table_id, tuple(idx.columns))
        if not idx.columns:
            continue
        if key in seen:
            table = s.table_by_id(idx.table_id)
            cols = ", ".join(
                (table.field_by_id(c).name if table and table.field_by_id(c) else c) for c in idx.columns
            )
            yield _i(
                "IDX010", InsightCategory.INDEX, InsightKind.FACT, InsightSeverity.WARNING,
                f"Redundant index on ({cols})",
                f"Indexes {seen[key]} and {idx.id} cover the same columns; the duplicate adds write "
                "cost and storage without speeding any read.",
                path=table.name if table else idx.table_id, entity_id=idx.id, table_id=idx.table_id,
                fix="Drop one of the duplicate indexes.",
            )
        else:
            seen[key] = idx.id


# --------------------------------------------------------------------------------------------------
# §2 — Design Warnings (facts, not guesses).
# --------------------------------------------------------------------------------------------------
def _resolve_physical(reg: TypeRegistry, field) -> dict | None:
    try:
        return reg.resolve(field).physical
    except (KeyError, UnsupportedPhysicalTypeError):
        return None


def _design_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Insight]:
    multi_table = len(s.logical.tables) > 1
    # tables touched by any relation (for the island check)
    connected: set[str] = set()
    for rel in s.logical.relations:
        connected.add(rel.from_table_id)
        if rel.to_table_id:
            connected.add(rel.to_table_id)
        if rel.through_table_id:
            connected.add(rel.through_table_id)

    for table in s.logical.tables:
        relaxed = (table.kind or "normal") in _RELAXED_KINDS

        # No primary key.
        if not table.primary_keys() and not relaxed:
            yield _i(
                "DSN001", InsightCategory.DESIGN, InsightKind.FACT, InsightSeverity.WARNING,
                f"Table {table.name} has no primary key",
                "Without a primary key there is no stable way to address a row; updates, deletes and "
                "foreign keys to this table all become ambiguous.",
                path=table.name, entity_id=table.id, table_id=table.id,
                fix="Add a primary-key column (e.g. an id).",
            )

        # varchar without a length bound.
        for field in table.fields:
            physical = _resolve_physical(reg, field)
            if physical is None:
                continue
            ptype = _physical_type(physical)
            if any(ptype.startswith(v) for v in _VARCHARISH) and not physical.get("length"):
                loc = f"{table.name}.{field.name}"
                yield _i(
                    "DSN002", InsightCategory.DESIGN, InsightKind.FACT, InsightSeverity.WARNING,
                    f"{loc} is a varchar with no length",
                    "A varchar without an explicit length has no upper bound; most engines require one "
                    "and an unbounded text column is better modelled as text.",
                    path=loc, entity_id=field.id, table_id=table.id, field_id=field.id,
                    fix="Set an explicit length (e.g. varchar(255)) or use the 'text' type.",
                )

        # Island table — present but wired to nothing. Maybe intentional (spec §2 → warning, not error).
        if multi_table and not relaxed and table.id not in connected:
            yield _i(
                "DSN005", InsightCategory.DESIGN, InsightKind.FACT, InsightSeverity.WARNING,
                f"Table {table.name} has no relations",
                "This table isn't connected to any other; that can be intentional (a lookup/config "
                "table) but is often a missing foreign key.",
                path=table.name, entity_id=table.id, table_id=table.id,
                fix="Add a relation, or leave it if the table is standalone by design.",
            )

    # FK whose declared type contradicts the primary key it references — the project's recurring bug
    # (spec §2). We only flag a *concrete* mismatch: a field explicitly typed (not the generic
    # 'foreign_key', which the Type System deliberately resolves to the PK's type). A uuid PK with an
    # integer-typed FK is the classic case the database itself would reject.
    for rel in s.logical.relations:
        fid = rel.foreign_key_field_id
        if not (fid and rel.to_table_id):
            continue
        located = s.field_by_id(fid)
        to_table = s.table_by_id(rel.to_table_id)
        if not located or to_table is None:
            continue
        fk_table, fk_field = located
        if fk_field.semantic_type == "foreign_key":
            continue  # the generic FK type inherits the PK's physical type — the supported path
        pks = to_table.primary_keys()
        if not pks:
            continue
        fk_phys = _resolve_physical(reg, fk_field)
        pk_phys = _resolve_physical(reg, pks[0])
        if not fk_phys or not pk_phys:
            continue
        if _physical_type(fk_phys) != _physical_type(pk_phys):
            loc = f"{fk_table.name}.{fk_field.name}"
            ref = f"{to_table.name}.{pks[0].name}"
            yield _i(
                "DSN003", InsightCategory.DESIGN, InsightKind.FACT, InsightSeverity.WARNING,
                f"Foreign key {loc} type mismatch",
                f"{loc} stores {_physical_label(fk_phys)} but references {ref}, whose key is "
                f"{_physical_label(pk_phys)}. A foreign key must use the same type as the key it "
                "points to (a uuid primary key needs a uuid foreign key) or the constraint is invalid.",
                path=loc, entity_id=fid, table_id=fk_table.id, field_id=fid,
                fix=f"Change {fk_field.name} to match {ref} ({_physical_type(pk_phys)}), "
                    "or use the generic 'foreign_key' type so it inherits automatically.",
            )

    # Relation with no ON DELETE behaviour — a fact, kept gentle (info) since the right action is
    # context-dependent (spec §2).
    fk_shaped = {"one_to_one", "one_to_many", "many_to_one"}
    for rel in s.logical.relations:
        if rel.type in fk_shaped and rel.foreign_key_field_id and not rel.on_delete:
            from_table = s.table_by_id(rel.from_table_id)
            to_table = s.table_by_id(rel.to_table_id) if rel.to_table_id else None
            label = rel.name or (
                f"{from_table.name} → {to_table.name}" if from_table and to_table else rel.id
            )
            yield _i(
                "DSN004", InsightCategory.DESIGN, InsightKind.FACT, InsightSeverity.INFO,
                f"Relation {label} has no ON DELETE rule",
                "Without an explicit ON DELETE (and ON UPDATE) the database uses its default "
                "(usually NO ACTION); spelling it out makes the deletion behaviour intentional.",
                path=label, entity_id=rel.id,
                fix="Set onDelete (e.g. cascade, restrict, set null).",
            )

    # Mixed naming conventions across the schema — guessy, so a suggestion at info level (spec §2).
    has_snake = has_camel = False
    snake_eg = camel_eg = ""
    for table in s.logical.tables:
        for nm in [table.name, *(f.name for f in table.fields)]:
            if "_" in nm and nm.islower():
                has_snake = True
                snake_eg = snake_eg or nm
            elif re.search(r"[a-z][A-Z]", nm):
                has_camel = True
                camel_eg = camel_eg or nm
    if has_snake and has_camel:
        yield _i(
            "DSN006", InsightCategory.DESIGN, InsightKind.SUGGESTION, InsightSeverity.INFO,
            "Inconsistent naming convention",
            f"The schema mixes snake_case (e.g. {snake_eg!r}) and camelCase (e.g. {camel_eg!r}); "
            "picking one convention makes queries and generated code more predictable.",
            fix="Standardise on one convention (snake_case is conventional for SQL).",
        )


# --------------------------------------------------------------------------------------------------
# §3 — Sensitive Field Detection.
# --------------------------------------------------------------------------------------------------
def _privacy_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Insight]:
    fk_field_ids = {rel.foreign_key_field_id for rel in s.logical.relations if rel.foreign_key_field_id}
    for table in s.logical.tables:
        for field in table.fields:
            loc = f"{table.name}.{field.name}"
            try:
                resolved = reg.resolve(field)
            except (KeyError, UnsupportedPhysicalTypeError):
                resolved = None

            pii = bool(resolved.privacy.get("pii")) if resolved else False
            sensitivity = resolved.privacy.get("sensitivity") if resolved else None

            # Certain layer: the semantic type itself is sensitive (spec §3) — already protected.
            if pii:
                yield _i(
                    "PRV001", InsightCategory.PRIVACY, InsightKind.FACT, InsightSeverity.INFO,
                    f"{loc} is sensitive data",
                    f"The {field.semantic_type!r} type is personally-identifiable"
                    f"{f' ({sensitivity} sensitivity)' if sensitivity and sensitivity != 'none' else ''}; "
                    "the Type System already applies its masking/handling rules.",
                    path=loc, entity_id=field.id, table_id=table.id, field_id=field.id,
                )
                continue

            # Heuristic layer: a generic column with a suspicious name (spec §3) — a guess to confirm.
            is_generic = (
                field.semantic_type in _GENERIC_TYPES
                or (resolved is not None and _physical_type(resolved.physical).startswith(_VARCHARISH))
            )
            if (
                is_generic
                and not field.is_primary_key
                and field.id not in fk_field_ids
                and _looks_sensitive(field.name)
            ):
                yield _i(
                    "PRV002", InsightCategory.PRIVACY, InsightKind.SUGGESTION, InsightSeverity.WARNING,
                    f"{loc} may be sensitive",
                    f"The name {field.name!r} suggests this column could hold personal or sensitive "
                    "data, but its type isn't marked as PII. Confirm to mark it sensitive so masking "
                    "and privacy handling apply.",
                    path=loc, entity_id=field.id, table_id=table.id, field_id=field.id,
                    fix="Mark the field sensitive (or switch to a specific type like phone/national_code).",
                    action=InsightAction(type="mark_sensitive", label="Mark sensitive",
                                         table_id=table.id, field_id=field.id, sensitivity="medium"),
                )


_RULE_PACKS = (_index_rules, _design_rules, _privacy_rules)


def analyze(schema: SchemaJson, *, registry: TypeRegistry | None = None) -> InsightReport:
    """Run every rule pack and return an :class:`InsightReport` (deterministic, spec §6)."""
    reg = registry or DEFAULT_REGISTRY
    insights: list[Insight] = []
    for pack in _RULE_PACKS:
        insights.extend(pack(schema, reg))

    summary: dict[str, int] = {k.value: 0 for k in InsightKind}
    summary.update({s.value: 0 for s in InsightSeverity})
    summary.update({c.value: 0 for c in InsightCategory})
    for ins in insights:
        summary[ins.kind.value] += 1
        summary[ins.severity.value] += 1
        summary[ins.category.value] += 1
    return InsightReport(insights=insights, summary=summary)
