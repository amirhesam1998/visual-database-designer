"""Three-Way Drift Detection — Designed ↔ Migrations ↔ Live (Milestone 2 §2).

The payoff of brownfield. Three sources of truth that *should* agree but rarely do:

* **Leg A — Designed:** the ``schema_json`` the team authored (intent; carries Stable IDs).
* **Leg B — Migrations:** what the migration files say the schema should be (built by applying them
  to a shadow database and importing it — same :mod:`app.core.importer` code, different source).
* **Leg C — Live:** what is actually running in production (introspected directly).

This module is **pure** — it takes three :class:`SchemaJson` objects and reports divergence. It never
touches a database (the importer / shadow applier do that) and it never fixes anything: every finding
carries a *suggested* reconciliation that a human must approve (AD-5). Output is machine-readable, with
a SARIF projection and an exit code so it drops into CI.

Reconciliation of identity (spec §2.3): Leg A has Stable IDs; B and C come from the importer keyed by
name. We match by name (and structurally for renames), with a confidence; high-confidence matches are
automatic, ambiguous ones are flagged for confirmation rather than guessed wrong. Comparison is keyed
by name, and FK column types are resolved through the shared Type-System step (M2 §7) so a designed
``foreign_key`` column does not show as spurious drift against a live ``uuid`` column.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.schema_json import SchemaJson
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, UnsupportedPhysicalTypeError, resolve_fk_physical

# Drift categories (the six scenarios of spec §2.4) + their CI severity. "error" fails the gate.
_SEVERITY: dict[str, str] = {
    "synced": "none",
    "migration_not_applied": "warning",
    "manual_prod_change": "error",
    "design_ahead_of_code": "note",
    "code_ahead_of_design": "warning",
    "migration_incomplete": "error",
    "drift": "warning",
}

_SUGGESTION: dict[str, dict[str, str]] = {
    "migration_not_applied": {"action": "apply_migration"},
    "manual_prod_change": {"action": "import_to_design"},
    "design_ahead_of_code": {"action": "generate_migration"},
    "code_ahead_of_design": {"action": "import_to_design"},
    "migration_incomplete": {"action": "apply_migration"},
}


class DriftEntry(BaseModel):
    entity: str  # "users" or "users.email"
    kind: str  # table | column | type
    status: dict[str, bool]  # {designed, migrations, live}
    category: str
    severity: str
    detail: str | None = None
    suggestion: dict[str, str] | None = None


class AmbiguousMatch(BaseModel):
    entity: str
    candidates: list[str]
    confidence: float
    reason: str


class ReconcileResult(BaseModel):
    matched: int = 0
    ambiguous: list[AmbiguousMatch] = Field(default_factory=list)
    canonical_ids: dict[str, str] = Field(default_factory=dict)  # table name → canonical Stable ID


class DriftReport(BaseModel):
    reconcile: ReconcileResult
    drift: list[DriftEntry] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        """0 if no error-severity drift, 1 otherwise (CI gate — critical drift fails the build)."""
        return 1 if any(d.severity == "error" for d in self.drift) else 0

    def to_sarif(self) -> dict[str, Any]:
        rule_ids = sorted({d.category for d in self.drift})
        level = {"error": "error", "warning": "warning", "note": "note", "none": "note"}
        return {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{
                "tool": {"driver": {"name": "vdb-drift", "informationUri": "https://visual-db-designer.dev",
                                    "rules": [{"id": rid} for rid in rule_ids]}},
                "results": [{
                    "ruleId": d.category,
                    "level": level.get(d.severity, "warning"),
                    "message": {"text": d.detail or f"{d.category} at {d.entity}"},
                    "properties": {"status": d.status, "suggestion": d.suggestion},
                    "locations": [{"logicalLocations": [{"name": d.entity}]}],
                } for d in self.drift if d.severity != "none"],
            }],
        }


# --------------------------------------------------------------------------------------------------
# Resolution helpers.
# --------------------------------------------------------------------------------------------------
def _render_phys(p: dict[str, Any]) -> str:
    base = str(p.get("type", "text"))
    if "length" in p:
        return f"{base}({p['length']})"
    if "precision" in p:
        return f"{base}({p['precision']},{p.get('scale', 0)})"
    if "dimension" in p:
        return f"{base}({p['dimension']})"
    return base


def _physical_map(schema: SchemaJson, reg: TypeRegistry, driver: str = "postgres") -> dict[tuple[str, str], str]:
    """``(table_name, column_name) → resolved physical type string``, FK columns resolved to their
    referenced PK type (shared Type-System step) so intent and reality compare on equal footing.

    Resolution is **driver-aware** (multi-driver milestone §2, the FK lesson): on MySQL a uuid PK is
    ``CHAR(36)`` and its FK inherits exactly that, so a designed ``uuid``/``foreign_key`` column does
    not show as spurious type drift against a live MySQL ``CHAR(36)`` column. All three legs are
    resolved with the same driver, so the comparison stays on equal footing."""
    fk_override = resolve_fk_physical(schema, driver, reg)
    out: dict[tuple[str, str], str] = {}
    for table in schema.logical.tables:
        for field in table.fields:
            if field.id in fk_override:
                out[(table.name, field.name)] = _render_phys(fk_override[field.id])
                continue
            try:
                out[(table.name, field.name)] = _render_phys(reg.resolve(field, driver).physical)
            except (KeyError, UnsupportedPhysicalTypeError):
                out[(table.name, field.name)] = field.semantic_type
    return out


def _tables(schema: SchemaJson | None) -> dict[str, Any]:
    return {t.name: t for t in schema.logical.tables} if schema else {}


def _columns(table: Any) -> set[str]:
    return {f.name for f in table.fields} if table is not None else set()


def _categorize(a: bool, b: bool, c: bool) -> str:
    """Map an A/B/C presence triple to one of the spec's six drift categories."""
    if a and b and c:
        return "synced"
    if a and b and not c:
        return "migration_not_applied"
    if not a and not b and c:
        return "manual_prod_change"
    if a and not b and not c:
        return "design_ahead_of_code"
    if not a and b and c:
        return "code_ahead_of_design"
    if not a and b and not c:
        return "code_ahead_of_design"  # migration written, neither designed nor applied yet
    if a and not b and c:
        return "manual_prod_change"  # in design + live but no migration trail
    return "drift"


# --------------------------------------------------------------------------------------------------
# Reconcile (spec §2.3).
# --------------------------------------------------------------------------------------------------
def reconcile(a: SchemaJson, b: SchemaJson | None, c: SchemaJson | None) -> ReconcileResult:
    ta, tb, tc = _tables(a), _tables(b), _tables(c)
    other_names = set(tb) | set(tc)
    matched = 0
    ambiguous: list[AmbiguousMatch] = []
    canonical: dict[str, str] = {}

    for name in sorted(set(ta) | other_names):
        # Canonical id: prefer the designed (Stable ID) leg, else whichever leg has the table.
        if name in ta:
            canonical[name] = ta[name].id
        elif name in tb:
            canonical[name] = tb[name].id
        elif name in tc:
            canonical[name] = tc[name].id

    for name in sorted(ta):
        if name in other_names:
            matched += 1  # exact-name match across legs → automatic, high confidence
            continue
        # No exact match: look for a structural twin (same column set) under a different name.
        a_cols = _columns(ta[name])
        twins = [
            other for other in sorted(other_names)
            if other not in ta and _jaccard(a_cols, _columns(tb.get(other) or tc.get(other))) >= 0.6
        ]
        if twins:
            ambiguous.append(AmbiguousMatch(
                entity=name, candidates=twins,
                confidence=round(max(_jaccard(a_cols, _columns(tb.get(t) or tc.get(t))) for t in twins), 2),
                reason="no exact name match; a structurally similar table exists — confirm before merging",
            ))
    return ReconcileResult(matched=matched, ambiguous=ambiguous, canonical_ids=canonical)


def _jaccard(x: set[str], y: set[str]) -> float:
    if not x and not y:
        return 0.0
    return len(x & y) / len(x | y)


# --------------------------------------------------------------------------------------------------
# Three-way drift (spec §2.4 / §2.5).
# --------------------------------------------------------------------------------------------------
def three_way_drift(
    designed: SchemaJson, migrations: SchemaJson | None, live: SchemaJson | None,
    *, registry: TypeRegistry | None = None, driver: str = "postgres",
) -> DriftReport:
    reg = registry or DEFAULT_REGISTRY
    rec = reconcile(designed, migrations, live)
    ta, tb, tc = _tables(designed), _tables(migrations), _tables(live)
    pa, pb, pc = (_physical_map(designed, reg, driver),
                  _physical_map(migrations, reg, driver) if migrations else {},
                  _physical_map(live, reg, driver) if live else {})

    entries: list[DriftEntry] = []
    for name in sorted(set(ta) | set(tb) | set(tc)):
        in_a, in_b, in_c = name in ta, name in tb, name in tc
        cat = _categorize(in_a, in_b, in_c)
        if not (in_a and in_b and in_c):
            entries.append(_entry(name, "table", in_a, in_b, in_c, cat,
                                  detail=f"Table {name!r} present in {_legs(in_a, in_b, in_c)}."))
            # When the table is missing from a leg we don't drill into its columns for that leg.
            continue

        # Table exists in all three → drill into columns / types (catches partial migrations §2.4).
        cols = _columns(ta[name]) | _columns(tb[name]) | _columns(tc[name])
        for col in sorted(cols):
            ca, cb, cc = (name, col) in pa, (name, col) in pb, (name, col) in pc
            if ca and cb and cc:
                # Present everywhere — is it the *same* type everywhere?
                va, vb, vc = pa[(name, col)], pb[(name, col)], pc[(name, col)]
                if va == vb == vc:
                    continue  # synced, no drift
                if va == vb and vc != va:
                    entries.append(DriftEntry(
                        entity=f"{name}.{col}", kind="type", status=_st(True, True, True),
                        category="manual_prod_change", severity=_SEVERITY["manual_prod_change"],
                        detail=f"{name}.{col} type diverged in live ({vc}) from designed/migrations ({va}).",
                        suggestion=_SUGGESTION["manual_prod_change"]))
                else:
                    entries.append(DriftEntry(
                        entity=f"{name}.{col}", kind="type", status=_st(True, True, True),
                        category="migration_incomplete", severity=_SEVERITY["migration_incomplete"],
                        detail=f"{name}.{col} has inconsistent types A={va} B={vb} C={vc}.",
                        suggestion=_SUGGESTION["migration_incomplete"]))
                continue
            ccat = _categorize(ca, cb, cc)
            if ccat == "synced":
                continue
            entries.append(_entry(f"{name}.{col}", "column", ca, cb, cc, ccat,
                                  detail=f"Column {name}.{col} present in {_legs(ca, cb, cc)}."))

    summary: dict[str, int] = {}
    for e in entries:
        summary[e.category] = summary.get(e.category, 0) + 1
    return DriftReport(reconcile=rec, drift=entries, summary=summary)


def _entry(entity: str, kind: str, a: bool, b: bool, c: bool, cat: str, *, detail: str) -> DriftEntry:
    return DriftEntry(entity=entity, kind=kind, status=_st(a, b, c), category=cat,
                      severity=_SEVERITY.get(cat, "warning"), detail=detail, suggestion=_SUGGESTION.get(cat))


def _st(a: bool, b: bool, c: bool) -> dict[str, bool]:
    return {"designed": a, "migrations": b, "live": c}


def _legs(a: bool, b: bool, c: bool) -> str:
    present = [n for n, p in (("designed", a), ("migrations", b), ("live", c)) if p]
    return ", ".join(present) if present else "no legs"
