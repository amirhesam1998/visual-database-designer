"""Unit tests — Design Session lifecycle + Approval Gate (Milestone 1 §4/§8).

Covers the happy path, every guard (state-machine transitions), all three approval gates
(approver identity, re-checked validation, unacknowledged critical risk), immutability of approved
sessions, the migration baseline reset on revise, and determinism of the migration artifact.
"""

from __future__ import annotations

import copy

import pytest

from app.core.design_session import (
    GateBlockedError,
    InvalidTransitionError,
    SessionState,
    SessionStore,
)

from .factory import canonical_schema


@pytest.fixture
def store():
    return SessionStore()


def _approved(store, schema=None):
    session = store.create(prd="shop")
    store.apply_schema(session.id, schema or canonical_schema())
    store.validate(session.id)
    store.submit(session.id)
    store.approve(session.id, approved_by="user_1")
    return session


# --- happy path -----------------------------------------------------------------------------------


def test_full_lifecycle_reaches_approved(store):
    session = store.create(prd="shop")
    assert session.state == SessionState.DRAFT.value
    store.apply_schema(session.id, canonical_schema())
    _s, outcome = store.validate(session.id)
    assert outcome.is_green and session.state == SessionState.VALIDATED.value
    store.submit(session.id)
    assert session.state == SessionState.PENDING_APPROVAL.value
    store.approve(session.id, approved_by="user_1")
    assert session.state == SessionState.APPROVED.value
    assert session.schema_version == "v1" and session.checksum.startswith("sha256:")


# --- transition guards ----------------------------------------------------------------------------


def test_submit_requires_validated(store):
    session = store.create()
    store.apply_schema(session.id, canonical_schema())
    with pytest.raises(InvalidTransitionError):
        store.submit(session.id)  # still draft, not validated


def test_approve_from_draft_is_blocked(store):
    session = store.create()
    store.apply_schema(session.id, canonical_schema())
    with pytest.raises(InvalidTransitionError):
        store.approve(session.id, approved_by="user_1")


def test_migration_and_handoff_require_approved(store):
    session = store.create()
    store.apply_schema(session.id, canonical_schema())
    with pytest.raises(InvalidTransitionError, match="not_approved"):
        store.migration(session.id)
    with pytest.raises(InvalidTransitionError, match="not_approved"):
        store.handoff(session.id)


# --- the approval gates ---------------------------------------------------------------------------


def test_broken_schema_never_validates(store):
    broken = canonical_schema()
    broken["logical"]["relations"][0]["toTableId"] = "tbl_missing01"  # REF002 error
    session = store.create()
    store.apply_schema(session.id, broken)
    _s, outcome = store.validate(session.id)
    assert not outcome.is_green
    assert session.state == SessionState.DRAFT.value  # stays in draft, cannot be submitted


def test_approve_rechecks_validation(store):
    """Gate 2: even after submit, a schema that became invalid is blocked at approve."""
    session = store.create()
    store.apply_schema(session.id, canonical_schema())
    store.validate(session.id)
    store.submit(session.id)
    # Tamper with the locked-in schema after submit (simulates drift); approve must re-validate.
    session.schema_doc["logical"]["relations"][0]["toTableId"] = "tbl_missing01"
    with pytest.raises(GateBlockedError) as exc:
        store.approve(session.id, approved_by="user_1")
    assert exc.value.reason == "validation_error"


def test_approve_requires_an_approver(store):
    session = store.create()
    store.apply_schema(session.id, canonical_schema())
    store.validate(session.id)
    store.submit(session.id)
    with pytest.raises(GateBlockedError, match="missing_approver"):
        store.approve(session.id, approved_by="")


def test_critical_risk_blocks_then_acknowledges(store):
    session = _approved(store)
    revision = store.revise(session.id)
    assert revision.state == SessionState.DRAFT.value
    # Drop a table relative to the approved baseline → a CRITICAL migration op.
    dropped = copy.deepcopy(session.schema_doc)
    dropped["logical"]["tables"] = [dropped["logical"]["tables"][0]]
    dropped["logical"]["relations"] = []
    dropped["physical"] = {"indexes": dropped["physical"]["indexes"]}
    dropped["semantic"] = {}
    store.apply_schema(revision.id, dropped)
    store.validate(revision.id)
    store.submit(revision.id)
    with pytest.raises(GateBlockedError) as exc:
        store.approve(revision.id, approved_by="user_1")
    assert exc.value.reason == "critical_migration_risk"
    assert any(b["op"] == "drop_table" for b in exc.value.blocking)
    # With explicit acknowledgement the same approve succeeds.
    store.approve(revision.id, approved_by="user_1", acknowledge_critical=True)
    assert revision.state == SessionState.APPROVED.value


# --- immutability + baseline ----------------------------------------------------------------------


def test_approved_session_is_immutable(store):
    session = _approved(store)
    with pytest.raises(InvalidTransitionError, match="immutable"):
        store.apply_schema(session.id, canonical_schema())


def test_revise_uses_approved_schema_as_baseline(store):
    session = _approved(store)
    revision = store.revise(session.id)
    # Re-approving an unchanged revision yields an empty migration (baseline == current).
    store.validate(revision.id)
    store.submit(revision.id)
    store.approve(revision.id, approved_by="user_1")
    assert store.migration(revision.id)["operations"] == []


# --- determinism ----------------------------------------------------------------------------------


def test_migration_is_deterministic(store):
    session = _approved(store)
    assert store.migration(session.id) == store.migration(session.id)


def test_handoff_carries_checksum_and_approved_status(store):
    session = _approved(store)
    handoff = store.handoff(session.id)
    assert handoff["status"] == "approved"
    assert handoff["checksum"] == session.checksum
    assert handoff["schemaVersion"] == "v1"
    assert "consumerGuard" in handoff
