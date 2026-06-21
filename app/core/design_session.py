"""Design Session lifecycle + Approval Gate — the orchestration spine of Milestone 1.

A design session is itself a small state machine (dog-fooding the State Machine Designer's idea)::

    draft ──validate──▶ validated ──submit──▶ pending_approval ──approve──▶ approved
      ▲                     │                        │
      └──── edit ───────────┘                        └── reject ──▶ draft

The transitions enforce the **approval gate** (spec §4 / §8). No handoff or migration artifact is
ever produced from a session that is not ``approved`` — that is the one and only door to the
downstream generators (AD-5: AI suggests, a human approves).

Everything here is pure Core (AD-4): it composes the existing engines (schema_json, validation,
diff, risk, sql_emitter) and a trivial in-memory store. The ``/design/*`` routes are a thin wrapper.
Deterministic: migration/handoff for the same approved schema are byte-identical (spec §10).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.core import diff as core_diff
from app.core import risk as core_risk
from app.core import schema_json as core_sj
from app.core import validation as core_validation
from app.core.ids import generate_ulid
from app.core.schema_json import CURRENT_FORMAT_VERSION
from app.core.sql_emitter import SUPPORTED_DRIVERS, emit_sql


class SessionState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"


# --------------------------------------------------------------------------------------------------
# Errors (mapped to HTTP status codes by the route layer).
# --------------------------------------------------------------------------------------------------
class SessionNotFoundError(KeyError):
    """No session with the given id (→ 404)."""


class InvalidTransitionError(Exception):
    """A transition was attempted from a state that does not allow it (→ 409)."""

    def __init__(self, message: str, *, state: str | None = None) -> None:
        self.message = message
        self.state = state
        super().__init__(message)


class GateBlockedError(Exception):
    """An approve was blocked by the gate (→ 409 with a machine-readable reason)."""

    def __init__(self, reason: str, *, blocking: list[dict[str, Any]] | None = None) -> None:
        self.reason = reason
        self.blocking = blocking or []
        super().__init__(reason)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _empty_schema() -> dict[str, Any]:
    """The greenfield baseline: a structurally-valid, empty schema_json (spec §5)."""
    return {"formatVersion": CURRENT_FORMAT_VERSION, "logical": {"tables": []}}


def _checksum(schema: dict[str, Any]) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _migration_driver(schema: core_sj.SchemaJson) -> str:
    """Milestone 1 emits Postgres only; risk/SQL share one driver (spec §7/§12)."""
    candidate = core_diff._driver(schema)
    return candidate if candidate in SUPPORTED_DRIVERS else "postgres"


class ValidationOutcome(BaseModel):
    structural_errors: list[str] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    sarif: dict[str, Any] = Field(default_factory=dict)
    is_green: bool = False  # no structural errors and no error-severity findings (warnings allowed)


class DesignSession(BaseModel):
    # An internal model; the routes shape the public JSON (see ``to_response``) so the wire format
    # matches the spec exactly (``sessionId`` camelCase, ``schema_json`` snake_case).
    id: str
    mode: str = "greenfield"
    state: str = SessionState.DRAFT.value
    prd: str | None = None
    # The working schema document (camelCase schema_json dict) and the migration baseline.
    # The baseline is parametrised (spec M2 §0): empty for greenfield, the approved schema for a
    # revise, the imported live schema for brownfield. ``schema_doc`` avoids shadowing
    # ``BaseModel.schema_json``. ``baseline_source`` records which of the three it is.
    schema_doc: dict[str, Any] = Field(default_factory=_empty_schema)
    baseline: dict[str, Any] = Field(default_factory=_empty_schema)
    baseline_source: str = "empty"  # empty | approved | import
    version_counter: int = 0
    schema_version: str | None = None
    checksum: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    last_validation: ValidationOutcome | None = None
    suggestion: dict[str, Any] | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def to_response(self) -> dict[str, Any]:
        """The spec-shaped session view returned by the ``/design/*`` endpoints."""
        body: dict[str, Any] = {
            "sessionId": self.id,
            "mode": self.mode,
            "state": self.state,
            "baselineSource": self.baseline_source,
            "schema_json": self.schema_doc,
        }
        for key, value in (
            ("schemaVersion", self.schema_version),
            ("checksum", self.checksum),
            ("approvedBy", self.approved_by),
            ("approvedAt", self.approved_at),
        ):
            if value is not None:
                body[key] = value
        return body


def _run_validation(schema_json: dict[str, Any]) -> tuple[ValidationOutcome, core_validation.ValidationReport]:
    data = core_sj.migrate(schema_json)
    structural = core_sj.validate_structure(data)
    report = core_validation.validate(core_sj.load(data, validate=False))
    outcome = ValidationOutcome(
        structural_errors=structural,
        summary=report.summary,
        sarif=report.to_sarif(),
        is_green=(not structural) and report.valid,
    )
    return outcome, report


def _build_migration(schema_json: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    base = core_sj.load(baseline, validate=False)
    target = core_sj.load(schema_json, validate=False)
    driver = _migration_driver(target)
    operations = core_diff.diff(base, target).op_dicts()
    risk = core_risk.analyze(operations, driver=driver)
    script = emit_sql(operations, target, driver=driver)
    return {
        "operations": operations,
        "risk": risk.model_dump(),
        "plan": _safe_plan(risk),
        "sql": {
            "driver": driver,
            "steps": script.up_statements(),
            "up": script.up_statements(),
            "down": script.down_statements(),
            "requiresBackup": script.requires_backup,
        },
    }


def _safe_plan(risk: core_risk.RiskReport) -> list[dict[str, Any]]:
    """Stepwise safe plan (expand/contract where needed) derived from the risk report (spec §6)."""
    plan: list[dict[str, Any]] = []
    for o in risk.operations:
        entry: dict[str, Any] = {
            "op": o.op,
            "target": o.target,
            "level": o.level,
            "reversible": o.reversible,
            "requiresBackup": o.requires_backup,
            "steps": o.safe_plan or [f"Apply {o.op}" + (f" on {o.target}" if o.target else "")],
        }
        if o.backfill:
            entry["backfill"] = o.backfill
        plan.append(entry)
    return plan


def _critical_blocking(migration: dict[str, Any]) -> list[dict[str, Any]]:
    """Operations at CRITICAL risk — these block approval unless explicitly acknowledged."""
    crit = core_risk.RiskLevel.CRITICAL.label
    return [
        {"op": o["op"], "target": o.get("target"), "level": o["level"]}
        for o in migration["risk"]["operations"]
        if o["level"] == crit
    ]


# --------------------------------------------------------------------------------------------------
# The store — in-memory, the only stateful piece (kept trivial on purpose; AD-4).
# --------------------------------------------------------------------------------------------------
class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, DesignSession] = {}

    # -- lifecycle ---------------------------------------------------------------------------------
    def create(self, *, mode: str = "greenfield", prd: str | None = None,
               schema_json: dict[str, Any] | None = None,
               baseline: dict[str, Any] | None = None,
               baseline_source: str | None = None) -> DesignSession:
        # Parametrised baseline (spec M2 §0): brownfield passes the imported schema as *both* the
        # initial draft and the baseline, so a diff/migration is the delta from the live database.
        # Greenfield leaves the baseline empty. The source defaults sensibly from whether a baseline
        # was supplied, but can be set explicitly (e.g. "import").
        source = baseline_source or ("import" if baseline is not None else "empty")
        session = DesignSession(
            id="ses_" + generate_ulid(),
            mode=mode,
            prd=prd,
            schema_doc=core_sj.migrate(schema_json) if schema_json else _empty_schema(),
            baseline=core_sj.migrate(baseline) if baseline else _empty_schema(),
            baseline_source=source,
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> DesignSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(session_id) from exc

    # -- editing (draft) ---------------------------------------------------------------------------
    def attach_suggestion(self, session_id: str, suggestion: dict[str, Any]) -> DesignSession:
        """Record an AI suggestion WITHOUT applying it (AD-5 — a human applies it explicitly)."""
        session = self.get(session_id)
        session.suggestion = suggestion
        session.updated_at = _now()
        return session

    def apply_schema(self, session_id: str, schema_json: dict[str, Any]) -> DesignSession:
        """Set the working schema (apply-suggestion / edit). Forbidden once approved (immutable)."""
        session = self.get(session_id)
        if session.state == SessionState.APPROVED.value:
            raise InvalidTransitionError(
                "approved sessions are immutable; call revise to start a new version",
                state=session.state,
            )
        session.schema_doc = core_sj.migrate(schema_json)
        session.state = SessionState.DRAFT.value  # any edit drops back to draft (must re-validate)
        session.last_validation = None
        session.updated_at = _now()
        return session

    def update_presentation(self, session_id: str, nodes: list[dict[str, Any]]) -> DesignSession:
        """Persist canvas layout into the ``presentation`` layer ONLY (Canvas M2 §4).

        Moving a table is *not* a schema change: the diff/risk engines ignore ``presentation``
        (spec-schema-json-format §1), so this deliberately does NOT touch ``state`` or
        ``last_validation`` and does NOT drop an approved session back to draft. It is the one
        edit that bypasses the re-validate cycle, by design.
        """
        session = self.get(session_id)
        doc = dict(session.schema_doc)
        presentation = dict(doc.get("presentation") or {})
        presentation["nodes"] = nodes
        doc["presentation"] = presentation
        session.schema_doc = doc
        session.updated_at = _now()
        return session

    # -- transitions -------------------------------------------------------------------------------
    def validate(self, session_id: str) -> tuple[DesignSession, ValidationOutcome]:
        session = self.get(session_id)
        if session.state == SessionState.APPROVED.value:
            raise InvalidTransitionError("cannot re-validate an approved session", state=session.state)
        outcome, _report = _run_validation(session.schema_doc)
        session.last_validation = outcome
        session.state = SessionState.VALIDATED.value if outcome.is_green else SessionState.DRAFT.value
        session.updated_at = _now()
        return session, outcome

    def submit(self, session_id: str) -> DesignSession:
        session = self.get(session_id)
        if session.state != SessionState.VALIDATED.value:
            raise InvalidTransitionError(
                f"submit requires state=validated, not {session.state}", state=session.state
            )
        session.state = SessionState.PENDING_APPROVAL.value
        session.updated_at = _now()
        return session

    def reject(self, session_id: str) -> DesignSession:
        session = self.get(session_id)
        if session.state != SessionState.PENDING_APPROVAL.value:
            raise InvalidTransitionError(
                f"reject requires state=pending_approval, not {session.state}", state=session.state
            )
        session.state = SessionState.DRAFT.value
        session.updated_at = _now()
        return session

    def approve(self, session_id: str, *, approved_by: str, acknowledge_critical: bool = False) -> DesignSession:
        session = self.get(session_id)
        if session.state != SessionState.PENDING_APPROVAL.value:
            raise InvalidTransitionError(
                f"approve requires state=pending_approval, not {session.state}", state=session.state
            )
        # Gate 1: a human with an identity must approve (permission hook — spec §4.2).
        if not approved_by:
            raise GateBlockedError("missing_approver")
        # Gate 2: re-check validation at the moment of approval (spec §4.3 / §8.3).
        outcome, _report = _run_validation(session.schema_doc)
        session.last_validation = outcome
        if not outcome.is_green:
            raise GateBlockedError("validation_error", blocking=[{"summary": outcome.summary}])
        # Gate 3: no unacknowledged critical migration risk (spec §4.1 / §8.4).
        migration = _build_migration(session.schema_doc, session.baseline)
        blocking = _critical_blocking(migration)
        if blocking and not acknowledge_critical:
            raise GateBlockedError("critical_migration_risk", blocking=blocking)

        # Lock + version + checksum (spec §8). The approved schema is now immutable.
        session.version_counter += 1
        session.state = SessionState.APPROVED.value
        session.schema_version = f"v{session.version_counter}"
        session.checksum = _checksum(session.schema_doc)
        session.approved_by = approved_by
        session.approved_at = _now()
        session.updated_at = session.approved_at
        return session

    def revise(self, session_id: str) -> DesignSession:
        """Start a new editable session from an approved one (spec §4 — no in-place edits).

        The approved schema becomes the new migration baseline, so a subsequent migration is a real
        delta (e.g. dropping a table now shows up as a critical operation).
        """
        session = self.get(session_id)
        if session.state != SessionState.APPROVED.value:
            raise InvalidTransitionError("revise requires an approved session", state=session.state)
        new = DesignSession(
            id="ses_" + generate_ulid(),
            mode=session.mode,
            prd=session.prd,
            schema_doc=json.loads(json.dumps(session.schema_doc)),
            baseline=json.loads(json.dumps(session.schema_doc)),
            baseline_source="approved",
            version_counter=session.version_counter,
        )
        self._sessions[new.id] = new
        return new

    # -- downstream artifacts (only the approved door) ---------------------------------------------
    def migration(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if session.state != SessionState.APPROVED.value:
            raise InvalidTransitionError("not_approved", state=session.state)
        return _build_migration(session.schema_doc, session.baseline)

    def handoff(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if session.state != SessionState.APPROVED.value:
            raise InvalidTransitionError("not_approved", state=session.state)
        migration = _build_migration(session.schema_doc, session.baseline)
        outcome, _report = _run_validation(session.schema_doc)
        return {
            "formatVersion": session.schema_doc.get("formatVersion", CURRENT_FORMAT_VERSION),
            "schemaVersion": session.schema_version,
            "status": session.state,
            "approvedBy": session.approved_by,
            "approvedAt": session.approved_at,
            "schema_json": session.schema_doc,
            "validation": {"summary": outcome.summary},
            "migration": {"plan": migration["plan"], "sql": migration["sql"], "risk": migration["risk"]},
            "checksum": session.checksum,
            "consumerGuard": (
                "Downstream consumers MUST verify status=='approved' and re-compute the checksum "
                "over schema_json before generating; reject the artifact otherwise."
            ),
        }
