"""Diff Engine — id-based, semantic, layer-aware (``docs/spec-diff-engine.md``).

Compares two ``schema_json`` documents and produces a typed, ordered **operation list** — the exact
contract the Migration Risk Analyzer consumes (spec-migration-risk-analyzer §1). Because every
entity has a Stable ID (AD-1), a rename is a first-class operation rather than a destructive
drop+add. The ``presentation`` layer is ignored entirely (AD-3): moving a node on the canvas
produces no operations.

Also provides a three-way diff (base ↔ A ↔ B) that *detects* conflicts at the field/relation/
transition granularity for Schema Branching (MVP: detect only, manual resolution).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.schema_json import Field_, SchemaJson, Table
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, UnsupportedPhysicalTypeError

# Execution-order phase per op (lower runs first). Builds go up, drops come back down in reverse.
_PHASE: dict[str, int] = {
    "add_table": 10,
    "add_column": 20,
    "add_enum_value": 25,
    "add_index": 40,
    "add_relation": 45,
    # alters (order among them is not safety-critical)
    "rename_table": 50, "change_table_meta": 50, "rename_column": 50, "change_type": 50,
    "change_semantic_type": 50, "set_not_null": 50, "drop_not_null": 50, "change_default": 50,
    "set_primary_key": 50, "change_index": 50, "change_relation": 50, "rename_enum_value": 50,
    "add_business_rule": 50, "change_business_rule": 50, "add_state": 50, "add_transition": 50,
    "change_state_machine": 50,
    # drops (reverse dependency order)
    "drop_transition": 60, "drop_state": 60, "drop_business_rule": 60,
    "drop_relation": 62,
    "drop_index": 70,
    "drop_enum_value": 75,
    "drop_column": 80,
    "drop_table": 90,
}

# Colour for the UI diff (spec §8): green add, red drop, yellow change, blue rename.
_COLOR: dict[str, str] = {"add": "green", "drop": "red", "rename": "blue", "change": "yellow", "set": "yellow"}


class Operation(BaseModel):
    op: str
    layer: str = "logical"
    table_id: str | None = None
    field_id: str | None = None
    entity_id: str | None = None  # relation/index/enum/state-machine/state/transition id
    name: str | None = None
    from_: Any = Field(default=None, alias="from")
    to: Any = None
    field: dict[str, Any] | None = None  # for add_column / add_table payloads
    columns: list[str] | None = None
    unique: bool | None = None
    details: dict[str, Any] | None = None

    # camelCase aliases so the dumped op list matches the Risk Analyzer's input contract
    # (tableId/fieldId/entityId/from/to). ``from`` is special-cased above (Python keyword).
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class DiffResult(BaseModel):
    from_version: str | None = None
    to_version: str | None = None
    operations: list[Operation] = Field(default_factory=list)
    changelog: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    colored: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def op_dicts(self) -> list[dict[str, Any]]:
        """The operation list as plain dicts — the Risk Analyzer's input."""
        return [o.to_dict() for o in self.operations]


def _color_for(op_name: str) -> str:
    return _COLOR.get(op_name.split("_", 1)[0], "yellow")


def _sort_key(o: Operation) -> tuple[int, str, str, str, str, str]:
    """Deterministic ordering key: phase first, then a stable tiebreaker on the op's identity."""
    return (
        _PHASE.get(o.op, 50),
        o.op,
        o.table_id or "",
        o.field_id or "",
        o.entity_id or "",
        str(o.name or o.to or ""),
    )


def _physical_string(field: Field_, driver: str, reg: TypeRegistry) -> str:
    """Render a field's resolved physical type as ``varchar(255)`` / ``numeric(12,2)`` / ``vector(1536)``."""
    try:
        p = reg.resolve(field, driver).physical
    except (KeyError, UnsupportedPhysicalTypeError):
        return field.semantic_type
    t = p.get("type", field.semantic_type)
    if "length" in p:
        return f"{t}({p['length']})"
    if "precision" in p:
        return f"{t}({p['precision']},{p.get('scale', 0)})"
    if "dimension" in p:
        return f"{t}({p['dimension']})"
    return str(t)


def _index_by_id(items: list[Any]) -> dict[str, Any]:
    return {it.id: it for it in items}


def _driver(s: SchemaJson) -> str:
    if s.meta:
        return s.meta.default_driver or s.meta.database_type or "postgres"
    return "postgres"


# --------------------------------------------------------------------------------------------------
# Two-way diff.
# --------------------------------------------------------------------------------------------------
def diff(from_schema: SchemaJson, to_schema: SchemaJson, *, registry: TypeRegistry | None = None) -> DiffResult:
    reg = registry or DEFAULT_REGISTRY
    driver = _driver(to_schema)
    ops: list[Operation] = []

    ops += _diff_tables(from_schema, to_schema, driver, reg)
    ops += _diff_relations(from_schema, to_schema)
    ops += _diff_indexes(from_schema, to_schema)
    ops += _diff_enums(from_schema, to_schema)
    ops += _diff_semantic(from_schema, to_schema)

    # Topological ordering by phase, with a fully deterministic tiebreaker so the operation list
    # (and therefore the downstream risk report + emitted SQL) is byte-identical across processes —
    # set-difference iteration order is otherwise hash-seed dependent (Milestone 1 §10 determinism).
    ops.sort(key=_sort_key)

    stats = {"added": 0, "removed": 0, "changed": 0, "renamed": 0}
    changelog: list[str] = []
    colored: list[dict[str, str]] = []
    for o in ops:
        head = o.op.split("_", 1)[0]
        if head == "add":
            stats["added"] += 1
        elif head == "drop":
            stats["removed"] += 1
        elif head == "rename":
            stats["renamed"] += 1
        else:
            stats["changed"] += 1
        line = _changelog_line(o)
        changelog.append(line)
        colored.append({"color": _color_for(o.op), "text": line})

    notes: list[str] = []
    if any(o.op == "add_table" for o in ops) and any(o.op == "add_relation" for o in ops):
        notes.append("Two-phase apply: create tables first, then add foreign keys (handles FK cycles).")

    meta_from = from_schema.meta.schema_version if from_schema.meta else None
    meta_to = to_schema.meta.schema_version if to_schema.meta else None
    return DiffResult(
        from_version=meta_from, to_version=meta_to,
        operations=ops, changelog=changelog, stats=stats, colored=colored, notes=notes,
    )


def _changelog_line(o: Operation) -> str:
    target = o.field_id or o.table_id or o.entity_id or ""
    if o.op in {"rename_table", "rename_column", "rename_enum_value"}:
        return f"{o.op}: {o.from_} → {o.to} ({target})"
    if o.op in {"change_type", "change_semantic_type", "change_default"}:
        return f"{o.op} on {target}: {o.from_} → {o.to}"
    if o.op == "add_table":
        return f"add_table {o.name}"
    if o.op == "add_column":
        return f"add_column {(o.field or {}).get('name', '')} to {o.table_id}"
    return f"{o.op} {target}".strip()


def _diff_tables(a: SchemaJson, b: SchemaJson, driver: str, reg: TypeRegistry) -> list[Operation]:
    ops: list[Operation] = []
    fa = _index_by_id(a.logical.tables)
    fb = _index_by_id(b.logical.tables)
    for tid in fb.keys() - fa.keys():
        t: Table = fb[tid]
        ops.append(Operation(op="add_table", table_id=tid, name=t.name,
                             field={"fields": len(t.fields)}))
    for tid in fa.keys() - fb.keys():
        ops.append(Operation(op="drop_table", table_id=tid, name=fa[tid].name))
    for tid in fa.keys() & fb.keys():
        ops += _diff_one_table(fa[tid], fb[tid], driver, reg)
    return ops


def _diff_one_table(ta: Table, tb: Table, driver: str, reg: TypeRegistry) -> list[Operation]:
    ops: list[Operation] = []
    if ta.name != tb.name:
        ops.append(Operation(op="rename_table", table_id=tb.id, from_=ta.name, to=tb.name))
    if (ta.kind, ta.domain, ta.comment) != (tb.kind, tb.domain, tb.comment):
        ops.append(Operation(op="change_table_meta", table_id=tb.id,
                             details={"kind": tb.kind, "domain": tb.domain}))

    fa = _index_by_id(ta.fields)
    fb = _index_by_id(tb.fields)
    for fid in fb.keys() - fa.keys():
        f: Field_ = fb[fid]
        ops.append(Operation(op="add_column", table_id=tb.id, field_id=fid,
                             field={"name": f.name, "semanticType": f.semantic_type,
                                    "nullable": f.nullable, "default": f.default}))
    for fid in fa.keys() - fb.keys():
        ops.append(Operation(op="drop_column", table_id=tb.id, field_id=fid, name=fa[fid].name))
    for fid in fa.keys() & fb.keys():
        ops += _diff_field(tb.id, fa[fid], fb[fid], driver, reg)
    return ops


def _diff_field(table_id: str, fa: Field_, fb: Field_, driver: str, reg: TypeRegistry) -> list[Operation]:
    ops: list[Operation] = []
    if fa.name != fb.name:
        ops.append(Operation(op="rename_column", table_id=table_id, field_id=fb.id, from_=fa.name, to=fb.name))
    if fa.semantic_type != fb.semantic_type:
        ops.append(Operation(op="change_semantic_type", table_id=table_id, field_id=fb.id,
                             from_=fa.semantic_type, to=fb.semantic_type))
    pa, pb = _physical_string(fa, driver, reg), _physical_string(fb, driver, reg)
    if pa != pb:
        ops.append(Operation(op="change_type", layer="physical", table_id=table_id, field_id=fb.id, from_=pa, to=pb))
    if fa.nullable and not fb.nullable:
        ops.append(Operation(op="set_not_null", table_id=table_id, field_id=fb.id))
    if not fa.nullable and fb.nullable:
        ops.append(Operation(op="drop_not_null", table_id=table_id, field_id=fb.id))
    if fa.default != fb.default:
        ops.append(Operation(op="change_default", table_id=table_id, field_id=fb.id, from_=fa.default, to=fb.default))
    if not fa.is_primary_key and fb.is_primary_key:
        ops.append(Operation(op="set_primary_key", table_id=table_id, field_id=fb.id))
    return ops


def _diff_relations(a: SchemaJson, b: SchemaJson) -> list[Operation]:
    ops: list[Operation] = []
    fa = _index_by_id(a.logical.relations)
    fb = _index_by_id(b.logical.relations)
    for rid in fb.keys() - fa.keys():
        ops.append(Operation(op="add_relation", entity_id=rid, name=fb[rid].type))
    for rid in fa.keys() - fb.keys():
        ops.append(Operation(op="drop_relation", entity_id=rid, name=fa[rid].type))
    for rid in fa.keys() & fb.keys():
        ra, rb = fa[rid], fb[rid]
        if (ra.type, ra.on_delete, ra.on_update, ra.to_table_id, ra.foreign_key_field_id) != (
            rb.type, rb.on_delete, rb.on_update, rb.to_table_id, rb.foreign_key_field_id
        ):
            ops.append(Operation(op="change_relation", entity_id=rid,
                                 details={"onDelete": rb.on_delete, "onUpdate": rb.on_update, "type": rb.type}))
    return ops


def _diff_indexes(a: SchemaJson, b: SchemaJson) -> list[Operation]:
    ops: list[Operation] = []
    ia = _index_by_id(a.physical.indexes) if a.physical else {}
    ib = _index_by_id(b.physical.indexes) if b.physical else {}
    for iid in ib.keys() - ia.keys():
        ix = ib[iid]
        ops.append(Operation(op="add_index", layer="physical", table_id=ix.table_id, entity_id=iid,
                             columns=list(ix.columns), unique=ix.unique))
    for iid in ia.keys() - ib.keys():
        ix = ia[iid]
        ops.append(Operation(op="drop_index", layer="physical", table_id=ix.table_id, entity_id=iid,
                             columns=list(ix.columns), unique=ix.unique))
    for iid in ia.keys() & ib.keys():
        if (ia[iid].columns, ia[iid].unique, ia[iid].type) != (ib[iid].columns, ib[iid].unique, ib[iid].type):
            ops.append(Operation(op="change_index", layer="physical", table_id=ib[iid].table_id, entity_id=iid,
                                 columns=list(ib[iid].columns), unique=ib[iid].unique))
    return ops


def _diff_enums(a: SchemaJson, b: SchemaJson) -> list[Operation]:
    ops: list[Operation] = []
    ea = _index_by_id(a.logical.enums)
    eb = _index_by_id(b.logical.enums)
    for eid in eb.keys() & ea.keys():
        va = {v.value for v in ea[eid].values}
        vb = {v.value for v in eb[eid].values}
        for value in vb - va:
            ops.append(Operation(op="add_enum_value", entity_id=eid, to=value))
        for value in va - vb:
            ops.append(Operation(op="drop_enum_value", entity_id=eid, from_=value))
    return ops


def _diff_semantic(a: SchemaJson, b: SchemaJson) -> list[Operation]:
    ops: list[Operation] = []
    sa, sb = a.semantic, b.semantic
    ra = _index_by_id(sa.business_rules) if sa else {}
    rb = _index_by_id(sb.business_rules) if sb else {}
    for rid in rb.keys() - ra.keys():
        ops.append(Operation(op="add_business_rule", layer="semantic", entity_id=rid))
    for rid in ra.keys() - rb.keys():
        ops.append(Operation(op="drop_business_rule", layer="semantic", entity_id=rid))
    for rid in ra.keys() & rb.keys():
        if ra[rid].model_dump() != rb[rid].model_dump():
            ops.append(Operation(op="change_business_rule", layer="semantic", entity_id=rid))

    ma = _index_by_id(sa.state_machines) if sa else {}
    mb = _index_by_id(sb.state_machines) if sb else {}
    for mid in ma.keys() & mb.keys():
        states_a = _index_by_id(ma[mid].states)
        states_b = _index_by_id(mb[mid].states)
        for sid in states_b.keys() - states_a.keys():
            ops.append(Operation(op="add_state", layer="semantic", entity_id=sid))
        for sid in states_a.keys() - states_b.keys():
            ops.append(Operation(op="drop_state", layer="semantic", entity_id=sid,
                                 details={"note": "removing a state drops an enum value (data-loss risk)"}))
        tr_a = _index_by_id(ma[mid].transitions)
        tr_b = _index_by_id(mb[mid].transitions)
        for tid in tr_b.keys() - tr_a.keys():
            ops.append(Operation(op="add_transition", layer="semantic", entity_id=tid))
        for tid in tr_a.keys() - tr_b.keys():
            ops.append(Operation(op="drop_transition", layer="semantic", entity_id=tid))
    return ops


# --------------------------------------------------------------------------------------------------
# Three-way diff (Branching MVP — detect conflicts only).
# --------------------------------------------------------------------------------------------------
class Conflict(BaseModel):
    entity_id: str
    attribute: str
    base: Any
    a: Any
    b: Any


class ThreeWayResult(BaseModel):
    conflicts: list[Conflict] = Field(default_factory=list)
    auto_apply_a: list[str] = Field(default_factory=list)  # op summaries unique to A
    auto_apply_b: list[str] = Field(default_factory=list)
    has_conflicts: bool = False


def _attribute_changes(base: SchemaJson, other: SchemaJson) -> dict[tuple[str, str], Any]:
    """Map (entity_id, attribute) → new value for every field/relation/transition attribute changed."""
    changes: dict[tuple[str, str], Any] = {}
    base_fields = {f.id: f for _t, f in base.all_fields()}
    for _t, f in other.all_fields():
        bf = base_fields.get(f.id)
        if bf is None:
            continue
        for attr in ("name", "semantic_type", "nullable", "default", "is_primary_key"):
            if getattr(bf, attr) != getattr(f, attr):
                changes[(f.id, attr)] = getattr(f, attr)
    base_rel = _index_by_id(base.logical.relations)
    for r in other.logical.relations:
        br = base_rel.get(r.id)
        if br is None:
            continue
        for attr in ("type", "on_delete", "on_update", "to_table_id"):
            if getattr(br, attr) != getattr(r, attr):
                changes[(r.id, attr)] = getattr(r, attr)
    base_tr = {tr.id: tr for sm in (base.semantic.state_machines if base.semantic else []) for tr in sm.transitions}
    for sm in (other.semantic.state_machines if other.semantic else []):
        for tr in sm.transitions:
            bt = base_tr.get(tr.id)
            if bt is None:
                continue
            for attr in ("from_", "to", "guard", "permission"):
                if getattr(bt, attr) != getattr(tr, attr):
                    changes[(tr.id, attr)] = getattr(tr, attr)
    return changes


def three_way_diff(base: SchemaJson, branch_a: SchemaJson, branch_b: SchemaJson) -> ThreeWayResult:
    """Detect conflicts where A and B change the *same* (entity, attribute) to *different* values."""
    ca = _attribute_changes(base, branch_a)
    cb = _attribute_changes(base, branch_b)
    base_fields = {f.id: f for _t, f in base.all_fields()}

    conflicts: list[Conflict] = []
    for key in ca.keys() & cb.keys():
        if ca[key] != cb[key]:
            eid, attr = key
            base_val = getattr(base_fields.get(eid), attr, None) if eid in base_fields else None
            conflicts.append(Conflict(entity_id=eid, attribute=attr, base=base_val, a=ca[key], b=cb[key]))

    auto_a = [f"{eid}.{attr}={val}" for (eid, attr), val in ca.items() if (eid, attr) not in cb]
    auto_b = [f"{eid}.{attr}={val}" for (eid, attr), val in cb.items() if (eid, attr) not in ca]
    return ThreeWayResult(
        conflicts=sorted(conflicts, key=lambda c: (c.entity_id, c.attribute)),
        auto_apply_a=sorted(auto_a), auto_apply_b=sorted(auto_b),
        has_conflicts=bool(conflicts),
    )
