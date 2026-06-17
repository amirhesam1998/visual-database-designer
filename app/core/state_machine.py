"""State Machine Designer — one definition, six deterministic outputs.

For a ``Status`` field, instead of a dumb enum we model real states/transitions/guards/permissions
(``docs/spec-state-machine-designer.md``). From that single definition the Core *deterministically*
derives: enum values, business-rule invariants, an allowed/disallowed transition test matrix, admin
status-change buttons, transition API endpoints, a consistency-aware seeder plan, and a Mermaid
``stateDiagram``. Core stays framework-neutral (AD-4): it emits a neutral model; adapters turn it
into Laravel/Prisma/etc.

Structural validation (one initial state, reachability, deadlock, …) lives in :func:`iter_findings`
and is reused by the Validation Engine so there is a single source of truth.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.core.schema_json import SchemaJson, StateMachine


@dataclass(frozen=True)
class SMFinding:
    code: str
    severity: str  # "error" | "warning"
    message: str
    entity_id: str | None = None


def iter_findings(sm: StateMachine) -> Iterator[SMFinding]:
    """Structural checks that depend only on the machine itself (spec §6).

    Field-binding checks (SM001/SM002) need schema context and live in the Validation Engine.
    """
    state_ids = {st.id for st in sm.states}
    initials = [st for st in sm.states if st.initial]
    if len(initials) != 1:
        yield SMFinding("SM003", "error",
                        f"State machine {sm.name} must have exactly one initial state (found {len(initials)}).", sm.id)
    if not any(st.final for st in sm.states):
        yield SMFinding("SM004", "warning", f"State machine {sm.name} has no final state.", sm.id)

    seen: set[tuple[str, str, str | None]] = set()
    outgoing: dict[str, list[str]] = {}
    for tr in sm.transitions:
        if tr.from_ not in state_ids:
            yield SMFinding("SM005", "error", f"Transition {tr.id} 'from' references an undefined state.", tr.id)
        if tr.to not in state_ids:
            yield SMFinding("SM006", "error", f"Transition {tr.id} 'to' references an undefined state.", tr.id)
        key = (tr.from_, tr.to, tr.name)
        if key in seen:
            yield SMFinding("SM007", "warning", f"Duplicate transition {tr.name} ({tr.from_}->{tr.to}).", tr.id)
        seen.add(key)
        outgoing.setdefault(tr.from_, []).append(tr.to)

    reachable = {st.id for st in initials}
    changed = True
    while changed:
        changed = False
        for src in list(reachable):
            for dst in outgoing.get(src, []):
                if dst not in reachable:
                    reachable.add(dst)
                    changed = True
    for st in sm.states:
        if st.id not in reachable and not st.initial:
            yield SMFinding("SM008", "warning", f"State {st.name} in {sm.name} is unreachable.", st.id)
        if not st.final and not outgoing.get(st.id):
            yield SMFinding("SM009", "warning",
                            f"State {st.name} in {sm.name} is a deadlock (non-final, no outgoing transitions).", st.id)


def is_valid(sm: StateMachine) -> bool:
    return not any(f.severity == "error" for f in iter_findings(sm))


# --------------------------------------------------------------------------------------------------
# Helpers shared by the derivations.
# --------------------------------------------------------------------------------------------------
def _state_name(sm: StateMachine, sid: str) -> str:
    st = sm.state_by_id(sid)
    return st.name if st else sid


def _resource_name(sm: StateMachine, schema: SchemaJson | None) -> str:
    if schema is not None:
        found = schema.field_by_id(sm.field_id)
        if found:
            return found[0].name
    return sm.name.lower()


# --------------------------------------------------------------------------------------------------
# The six deterministic derivations (spec §3).
# --------------------------------------------------------------------------------------------------
def enum_values(sm: StateMachine) -> list[str]:
    """Enum/DB: the enum values are exactly the state names, in declaration order."""
    return [st.name for st in sm.states]


def neutral_model(sm: StateMachine, schema: SchemaJson | None = None) -> dict[str, Any]:
    """Framework-neutral transition model (spec §8) — what adapters consume."""
    initial = next((st.name for st in sm.states if st.initial), None)
    return {
        "name": sm.name,
        "field": _resource_name(sm, schema) if schema else sm.field_id,
        "states": [st.name for st in sm.states],
        "initial": initial,
        "final": [st.name for st in sm.states if st.final],
        "transitions": [
            {
                "name": tr.name or f"{_state_name(sm, tr.from_)}_to_{_state_name(sm, tr.to)}",
                "from": _state_name(sm, tr.from_),
                "to": _state_name(sm, tr.to),
                "guard": tr.guard,
                "permission": tr.permission,
                "sideEffects": tr.side_effects or [],
            }
            for tr in sm.transitions
        ],
    }


def business_rules(sm: StateMachine) -> list[dict[str, Any]]:
    """Business Rules: one invariant per transition + one global 'allowed set' invariant."""
    rules: list[dict[str, Any]] = []
    for tr in sm.transitions:
        frm, to = _state_name(sm, tr.from_), _state_name(sm, tr.to)
        rules.append({
            "id": f"{sm.id}.{tr.id}",
            "category": "invariant",
            "intent": f"{sm.name}: '{tr.name}' transitions {frm} → {to}"
            + (f" (requires permission {tr.permission})" if tr.permission else ""),
            "structured": {"transition": tr.name, "from": frm, "to": to, "guard": tr.guard,
                           "permission": tr.permission},
        })
    allowed = sorted({(_state_name(sm, t.from_), _state_name(sm, t.to)) for t in sm.transitions})
    rules.append({
        "id": f"{sm.id}.invariant",
        "category": "invariant",
        "intent": f"{sm.name}: status may only change along defined transitions.",
        "structured": {"allowed": [f"{a}->{b}" for a, b in allowed]},
    })
    return rules


def test_matrix(sm: StateMachine) -> list[dict[str, Any]]:
    """Test Generator: an 'allow' test per real transition + a 'reject' test per missing pair."""
    defined = {(t.from_, t.to) for t in sm.transitions}
    tests: list[dict[str, Any]] = []
    for tr in sm.transitions:
        tests.append({
            "name": f"allows_{tr.name}",
            "from": _state_name(sm, tr.from_), "to": _state_name(sm, tr.to),
            "transition": tr.name, "expect": "allow",
        })
    for src in sm.states:
        for dst in sm.states:
            if src.id == dst.id or (src.id, dst.id) in defined:
                continue
            tests.append({
                "name": f"rejects_{src.name}_to_{dst.name}",
                "from": src.name, "to": dst.name, "transition": None, "expect": "reject",
            })
    return tests


def admin_buttons(sm: StateMachine) -> dict[str, list[str]]:
    """Admin Generator: which transition buttons to show for each current state."""
    buttons: dict[str, list[str]] = {st.name: [] for st in sm.states}
    for tr in sm.transitions:
        buttons[_state_name(sm, tr.from_)].append(tr.name or _state_name(sm, tr.to))
    return buttons


def api_endpoints(sm: StateMachine, schema: SchemaJson | None = None) -> list[dict[str, Any]]:
    """API: a dedicated transition endpoint per transition (no free-form status update)."""
    resource = _resource_name(sm, schema)
    return [
        {
            "method": "POST",
            "path": f"/{resource}/{{id}}/{tr.name}",
            "transition": tr.name,
            "permission": tr.permission,
        }
        for tr in sm.transitions
    ]


def seeder_plan(sm: StateMachine) -> dict[str, list[str]]:
    """Seeder: the shortest transition path from the initial state to each state.

    A seeder must not fabricate inconsistent data (a 'shipped' order must have been 'paid' first), so
    it replays these transitions instead of setting the status directly.
    """
    initial = next((st for st in sm.states if st.initial), None)
    if initial is None:
        return {}
    adj: dict[str, list[tuple[str, str]]] = {}
    for tr in sm.transitions:
        adj.setdefault(tr.from_, []).append((tr.to, tr.name or _state_name(sm, tr.to)))

    paths: dict[str, list[str]] = {initial.name: []}
    queue: deque[tuple[str, list[str]]] = deque([(initial.id, [])])
    visited = {initial.id}
    while queue:
        sid, path = queue.popleft()
        for to_id, tname in adj.get(sid, []):
            if to_id in visited:
                continue
            visited.add(to_id)
            new_path = [*path, tname]
            paths[_state_name(sm, to_id)] = new_path
            queue.append((to_id, new_path))
    return paths


def mermaid(sm: StateMachine) -> str:
    """Docs: a Mermaid stateDiagram-v2 (spec §3 example)."""
    lines = ["stateDiagram-v2"]
    initial = next((st for st in sm.states if st.initial), None)
    if initial:
        lines.append(f"    [*] --> {initial.name}")
    for tr in sm.transitions:
        frm, to = _state_name(sm, tr.from_), _state_name(sm, tr.to)
        label = f": {tr.name}" if tr.name else ""
        lines.append(f"    {frm} --> {to}{label}")
    for st in sm.states:
        if st.final:
            lines.append(f"    {st.name} --> [*]")
    return "\n".join(lines)


def derive_all(sm: StateMachine, schema: SchemaJson | None = None) -> dict[str, Any]:
    """Bundle every derivation — the essence of the feature: one definition, six outputs."""
    return {
        "valid": is_valid(sm),
        "findings": [f.__dict__ for f in iter_findings(sm)],
        "enum": enum_values(sm),
        "neutral_model": neutral_model(sm, schema),
        "business_rules": business_rules(sm),
        "tests": test_matrix(sm),
        "admin_buttons": admin_buttons(sm),
        "api_endpoints": api_endpoints(sm, schema),
        "seeder_plan": seeder_plan(sm),
        "mermaid": mermaid(sm),
    }
