"""Validation Engine — deterministic schema quality + referential integrity.

This is the line between "toy" and "production tool" (``docs/README.md`` §5). It catches the things
the JSON Schema deliberately does *not* (referential integrity — spec-schema-json-format §10) plus a
pack of quality/security/performance rules (``docs/01-core-foundation.md`` §5).

Every finding has a stable ``rule_id``, a ``severity``, a human ``message`` and (often) a ``fix``
hint, and can be suppressed with ``vdb-ignore: <rule-id> [reason]`` — either globally (passed in) or
inline in an entity ``comment``. Output serialises to **SARIF v2.1.0** so it drops straight into CI
code-scanning.

It is deterministic and LLM-free; state-machine rule logic is shared with
:mod:`app.core.state_machine`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.schema_json import SchemaJson
from app.core.state_machine import iter_findings as sm_findings
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry

_IGNORE_RE = re.compile(r"vdb-ignore:\s*([A-Za-z0-9_.\-]+)(?:\s+(.*))?")
_MONEY_NAMES = {"price", "total", "amount", "cost", "balance", "fee", "salary", "subtotal", "payment"}
# Semantic types that genuinely store money as a precise decimal.
_FLOATY_TYPES = {"float", "double", "real"}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    SUGGESTION = "suggestion"
    SECURITY = "security"
    PERFORMANCE = "performance"

    @property
    def sarif_level(self) -> str:
        return {
            Severity.ERROR: "error",
            Severity.SECURITY: "error",
            Severity.WARNING: "warning",
            Severity.PERFORMANCE: "warning",
            Severity.SUGGESTION: "note",
        }[self]


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    path: str = ""  # human-readable location, e.g. "orders.total"
    entity_id: str | None = None
    fix: str | None = None


def _f(rule_id: str, severity: Severity, message: str, *, path: str = "", entity_id: str | None = None,
       fix: str | None = None) -> Finding:
    """Positional shorthand for constructing a Finding (keeps the rule packs terse)."""
    return Finding(rule_id=rule_id, severity=severity, message=message, path=path, entity_id=entity_id, fix=fix)


class ValidationReport(BaseModel):
    valid: bool
    findings: list[Finding] = Field(default_factory=list)
    suppressed: int = 0
    summary: dict[str, int] = Field(default_factory=dict)

    def to_sarif(self) -> dict:
        """SARIF v2.1.0 log — one rule per distinct rule_id, one result per finding."""
        rule_ids = sorted({f.rule_id for f in self.findings})
        return {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "vdb-validate",
                            "informationUri": "https://visual-db-designer.dev",
                            "rules": [{"id": rid} for rid in rule_ids],
                        }
                    },
                    "results": [
                        {
                            "ruleId": f.rule_id,
                            "level": f.severity.sarif_level,
                            "message": {"text": f.message},
                            "properties": {"severity": f.severity.value, "fix": f.fix},
                            "locations": [
                                {"logicalLocations": [{"name": f.path or f.entity_id or "<root>"}]}
                            ],
                        }
                        for f in self.findings
                    ],
                }
            ],
        }


# --------------------------------------------------------------------------------------------------
# The rule packs. Each is a generator yielding Findings; the engine collects + suppresses.
# --------------------------------------------------------------------------------------------------
def _referential_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Finding]:
    table_ids = {t.id for t in s.logical.tables}
    field_ids = {f.id for _t, f in s.all_fields()}
    enum_ids = {e.id for e in s.logical.enums}

    # relations
    for rel in s.logical.relations:
        if rel.from_table_id not in table_ids:
            yield _f("REF001", Severity.ERROR, f"Relation {rel.id} fromTableId references a missing table.",
                          entity_id=rel.id)
        if rel.to_table_id and rel.to_table_id not in table_ids:
            yield _f("REF002", Severity.ERROR, f"Relation {rel.id} toTableId references a missing table.",
                          entity_id=rel.id)
        if rel.through_table_id and rel.through_table_id not in table_ids:
            yield _f("REF003", Severity.ERROR, f"Relation {rel.id} throughTableId references a missing table.",
                          entity_id=rel.id)
        if rel.foreign_key_field_id and rel.foreign_key_field_id not in field_ids:
            yield _f("REF004", Severity.ERROR, f"Relation {rel.id} foreignKeyFieldId references a missing field.",
                          entity_id=rel.id)

    # indexes: tableId exists, every column is a field of THAT table
    for idx in (s.physical.indexes if s.physical else []):
        table = s.table_by_id(idx.table_id)
        if table is None:
            yield _f("REF010", Severity.ERROR, f"Index {idx.id} tableId references a missing table.",
                          entity_id=idx.id)
            continue
        owned = {f.id for f in table.fields}
        for col in idx.columns:
            if col not in owned:
                yield _f("REF011", Severity.ERROR,
                              f"Index {idx.id} column {col} is not a field of table {table.name}.", entity_id=idx.id)

    # enum references
    for table, field in s.all_fields():
        resolves_to_enum = reg.has(field.semantic_type) and reg.get(field.semantic_type).category == "choice"
        if field.enum_id and field.enum_id not in enum_ids:
            yield _f("REF020", Severity.ERROR,
                          f"Field {table.name}.{field.name} enumId references a missing enum.",
                          path=f"{table.name}.{field.name}", entity_id=field.id)
        if resolves_to_enum and field.semantic_type == "enum" and not field.enum_id:
            yield _f("REF021", Severity.WARNING,
                          f"Field {table.name}.{field.name} is an enum but has no enumId / values.",
                          path=f"{table.name}.{field.name}", entity_id=field.id,
                          fix="Attach an enum definition (enumId) or convert to a state machine.")

    # semantic ownership / tenancy field references
    if s.semantic:
        for tid, fid in (s.semantic.ownership or {}).items():
            if tid not in table_ids:
                yield _f("REF030", Severity.WARNING, f"Ownership references missing table {tid}.")
            if fid not in field_ids:
                yield _f("REF031", Severity.WARNING, f"Ownership owner field {fid} does not exist.")
        tenancy = s.semantic.tenancy
        if tenancy and tenancy.tenant_key_field_by_table:
            for fid in tenancy.tenant_key_field_by_table.values():
                if fid not in field_ids:
                    yield _f("REF032", Severity.WARNING, f"Tenant key field {fid} does not exist.")


def _state_machine_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Finding]:
    if not s.semantic:
        return
    field_ids = {f.id for _t, f in s.all_fields()}
    for sm in s.semantic.state_machines:
        # Field-binding checks need schema context, so they live here (SM001/SM002).
        bound = s.field_by_id(sm.field_id)
        if sm.field_id not in field_ids:
            yield _f("SM001", Severity.ERROR, f"State machine {sm.name} fieldId references a missing field.",
                     entity_id=sm.id)
        elif bound and bound[1].semantic_type not in {"status", "enum"}:
            yield _f("SM002", Severity.ERROR,
                     f"State machine {sm.name} is bound to a non-Status field ({bound[1].name}).",
                     entity_id=sm.id, fix="Set the field's semanticType to 'status'.")

        # The structural checks (SM003..SM009) are owned by app.core.state_machine — single source.
        for fnd in sm_findings(sm):
            yield _f(fnd.code, Severity(fnd.severity), fnd.message, entity_id=fnd.entity_id)


def _quality_rules(s: SchemaJson, reg: TypeRegistry) -> Iterator[Finding]:
    unique_index_cols: set[str] = set()
    indexed_cols: set[str] = set()
    for idx in (s.physical.indexes if s.physical else []):
        for col in idx.columns:
            indexed_cols.add(col)
            if idx.unique and len(idx.columns) == 1:
                unique_index_cols.add(col)

    fk_field_ids = {rel.foreign_key_field_id for rel in s.logical.relations if rel.foreign_key_field_id}

    for table in s.logical.tables:
        # primary key
        if not table.primary_keys() and table.kind not in {"pivot", "log"}:
            yield _f("QLT001", Severity.WARNING, f"Table {table.name} has no primary key.",
                          path=table.name, entity_id=table.id, fix="Add a primary-key field (e.g. id).")
        # duplicate field names
        seen: set[str] = set()
        for f in table.fields:
            if f.name in seen:
                yield _f("QLT002", Severity.ERROR, f"Table {table.name} has a duplicate field name {f.name!r}.",
                              path=f"{table.name}.{f.name}", entity_id=f.id)
            seen.add(f.name)

        for f in table.fields:
            loc = f"{table.name}.{f.name}"
            stype = f.semantic_type
            # email should be unique
            if stype == "email" and f.id not in unique_index_cols:
                yield _f("QLT010", Severity.WARNING, f"Email field {loc} is not covered by a unique index.",
                              path=loc, entity_id=f.id, fix="Add a unique index on this column.")
            # money must not be a float
            phys_override = (f.overrides.physical or {}) if f.overrides else {}
            if f.name.lower() in _MONEY_NAMES and (
                stype in _FLOATY_TYPES or str(phys_override.get("type", "")).lower() in _FLOATY_TYPES
            ):
                yield _f("QLT011", Severity.WARNING, f"Financial field {loc} should be decimal, not float.",
                              path=loc, entity_id=f.id, fix="Use the 'money' or 'decimal' semantic type.")
            # password must be protected
            if stype == "password":
                masking = reg.get("password").api_masking if reg.has("password") else None
                if not masking:
                    yield _f("SEC001", Severity.SECURITY, f"Password field {loc} is exposed.",
                                  path=loc, entity_id=f.id)
            # unknown semantic type
            if not reg.has(stype):
                yield _f("QLT020", Severity.WARNING, f"Field {loc} uses an unregistered semantic type {stype!r}.",
                              path=loc, entity_id=f.id)
            # FK should be indexed (performance)
            if f.id in fk_field_ids and f.id not in indexed_cols:
                yield _f("PRF001", Severity.PERFORMANCE, f"Foreign-key field {loc} is not indexed.",
                              path=loc, entity_id=f.id, fix="Add an index on the foreign-key column.")

    # relations without an FK field (for FK-shaped relation types)
    fk_types = {"one_to_one", "one_to_many", "many_to_one"}
    for rel in s.logical.relations:
        if rel.type in fk_types and not rel.foreign_key_field_id:
            yield _f("QLT030", Severity.SUGGESTION,
                          f"Relation {rel.id} ({rel.type}) has no explicit foreign-key field.", entity_id=rel.id)


_RULE_PACKS = (_referential_rules, _state_machine_rules, _quality_rules)


# --------------------------------------------------------------------------------------------------
# Engine entry point.
# --------------------------------------------------------------------------------------------------
def _collect_inline_ignores(s: SchemaJson) -> dict[str | None, set[str]]:
    """Scan entity comments for ``vdb-ignore: <rule-id>``. Keyed by entity id (None == global/meta)."""
    ignores: dict[str | None, set[str]] = {}

    def scan(text: str | None, entity_id: str | None) -> None:
        if not text:
            return
        for rid, _reason in _IGNORE_RE.findall(text):
            ignores.setdefault(entity_id, set()).add(rid)

    if s.meta and s.meta.description:
        scan(s.meta.description, None)
    for table in s.logical.tables:
        scan(table.comment, table.id)
        for f in table.fields:
            scan(f.comment, f.id)
    return ignores


def validate(
    schema: SchemaJson,
    *,
    registry: TypeRegistry | None = None,
    ignore: dict[str | None, set[str]] | set[str] | None = None,
) -> ValidationReport:
    """Run every rule pack and return a :class:`ValidationReport`.

    ``ignore`` may be a global set of rule ids, or a mapping ``{entity_id|None: {rule_id}}``.
    Inline ``vdb-ignore:`` comments are merged in automatically.
    """
    reg = registry or DEFAULT_REGISTRY
    inline = _collect_inline_ignores(schema)

    global_ignores: set[str] = set()
    scoped_ignores: dict[str | None, set[str]] = {k: set(v) for k, v in inline.items()}
    if isinstance(ignore, set):
        global_ignores |= ignore
    elif isinstance(ignore, dict):
        for k, v in ignore.items():
            scoped_ignores.setdefault(k, set()).update(v)

    findings: list[Finding] = []
    suppressed = 0
    for pack in _RULE_PACKS:
        for finding in pack(schema, reg):
            if finding.rule_id in global_ignores or finding.rule_id in scoped_ignores.get(finding.entity_id, set()):
                suppressed += 1
                continue
            findings.append(finding)

    summary: dict[str, int] = {sev.value: 0 for sev in Severity}
    for f in findings:
        summary[f.severity.value] += 1
    valid = summary[Severity.ERROR.value] == 0

    return ValidationReport(valid=valid, findings=findings, suppressed=suppressed, summary=summary)
