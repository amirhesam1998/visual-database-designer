"""Conformance kit — State Machine Designer (``docs/spec-state-machine-designer.md`` §10).

Valid/invalid machines (no/multi initial, unreachable, deadlock, undefined target); the reference
machine derives the right enum, business rules, transition test matrix (incl. the spec's
cancelled→shipped 'must reject'), admin buttons, API endpoints, a consistency-aware seeder plan and
the documented Mermaid diagram.
"""

from __future__ import annotations

import pytest

from app.core import state_machine as SM  # noqa: N812 - SM reads naturally for a state machine
from app.core.schema_json import StateMachine

from .factory import order_state_machine


@pytest.fixture
def sm() -> StateMachine:
    return StateMachine.model_validate(order_state_machine())


def _codes(machine: StateMachine) -> set[str]:
    return {f.code for f in SM.iter_findings(machine)}


# --- validation (spec §6) -------------------------------------------------------------------------


def test_reference_machine_is_valid(sm):
    assert SM.is_valid(sm)
    assert _codes(sm) == set()


def test_no_initial_state(sm):
    raw = order_state_machine()
    raw["states"][0]["initial"] = False
    assert "SM003" in _codes(StateMachine.model_validate(raw))


def test_multiple_initial_states(sm):
    raw = order_state_machine()
    raw["states"][1]["initial"] = True
    assert "SM003" in _codes(StateMachine.model_validate(raw))


def test_unreachable_and_deadlock():
    raw = order_state_machine()
    raw["states"].append({"id": "stt_island01", "name": "island"})
    codes = _codes(StateMachine.model_validate(raw))
    assert "SM008" in codes and "SM009" in codes


def test_transition_to_undefined_state():
    raw = order_state_machine()
    raw["transitions"][0]["to"] = "stt_ghost0001"
    assert "SM006" in _codes(StateMachine.model_validate(raw))


def test_no_final_state_warns():
    raw = order_state_machine()
    for st in raw["states"]:
        st.pop("final", None)
    assert "SM004" in _codes(StateMachine.model_validate(raw))


# --- the six derivations (spec §3) ----------------------------------------------------------------


def test_enum_values_are_state_names(sm):
    assert SM.enum_values(sm) == ["pending", "paid", "shipped", "cancelled"]


def test_business_rules_one_per_transition_plus_global(sm):
    rules = SM.business_rules(sm)
    assert len(rules) == len(sm.transitions) + 1
    assert any("allowed" in r["structured"] for r in rules)


def test_test_matrix_allows_real_and_rejects_missing(sm):
    tests = SM.test_matrix(sm)
    allow = {t["transition"] for t in tests if t["expect"] == "allow"}
    reject = {(t["from"], t["to"]) for t in tests if t["expect"] == "reject"}
    assert allow == {"pay", "ship", "cancel"}
    # the canonical illegal move from the spec must be a 'reject' test
    assert ("cancelled", "shipped") in reject
    assert ("pending", "shipped") in reject


def test_admin_buttons_only_show_allowed(sm):
    buttons = SM.admin_buttons(sm)
    assert buttons["pending"] == ["pay", "cancel"]
    assert buttons["shipped"] == []  # final state, no outgoing


def test_api_endpoints_are_transition_specific(sm):
    paths = {e["path"] for e in SM.api_endpoints(sm)}
    assert "/orderstatus/{id}/pay" in paths
    assert all(e["method"] == "POST" for e in SM.api_endpoints(sm))


def test_seeder_plan_is_consistency_aware(sm):
    plan = SM.seeder_plan(sm)
    assert plan["pending"] == []
    assert plan["paid"] == ["pay"]
    assert plan["shipped"] == ["pay", "ship"]  # a shipped order must have been paid first


def test_mermaid_diagram_matches_spec(sm):
    diagram = SM.mermaid(sm)
    assert diagram.startswith("stateDiagram-v2")
    assert "[*] --> pending" in diagram
    assert "pending --> paid: pay" in diagram
    assert "shipped --> [*]" in diagram


def test_neutral_model_is_framework_neutral(sm):
    model = SM.neutral_model(sm)
    assert model["initial"] == "pending"
    assert {t["name"] for t in model["transitions"]} == {"pay", "ship", "cancel"}
    cancel = next(t for t in model["transitions"] if t["name"] == "cancel")
    assert cancel["sideEffects"] == ["release_stock"]


def test_derive_all_bundles_everything(sm):
    bundle = SM.derive_all(sm)
    assert bundle["valid"] is True
    assert set(bundle) >= {"enum", "business_rules", "tests", "admin_buttons", "api_endpoints",
                           "seeder_plan", "mermaid", "neutral_model", "findings"}
