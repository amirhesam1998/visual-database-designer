"""Interactive REST + canvas routes (tasks 6F.6, 6F.7).

The Module Protocol gives us /manifest, /health and /run for the pipeline. The Visual Database
Designer is *also* an interactive tool, so we register a few extra endpoints on the same FastAPI app
for the drag & drop canvas to call directly:

  POST /design          — feature_request → database_schema (validated + exported)
  POST /validate        — schema → validation report
  POST /export          — schema + type → one artifact (canonical + framework schemas)
  POST /import          — SQL dump / Laravel migration → database_schema
  POST /generate/model  — schema + framework + table → ORM model/entity class (#21)
  POST /generate/crud   — schema + framework + table + methods → CRUD controller (#22)
  GET  /frameworks      — supported export/model/crud frameworks (for the UI dropdowns)
  GET  /field-presets   — common-column suggestions for the canvas (#12)
  POST /compare         — {old, new} → {diff, migration} (schema versioning, #9)
  GET  /canvas          — the drag & drop designer page (static frontend)

These are additive and standalone-friendly: /design synthesizes an LLM client from the module's own
environment (falling back to the offline heuristic when no LLM is configured).
"""

from __future__ import annotations

from pathlib import Path

from aiarch_module_sdk import LLMClient
from aiarch_module_sdk.standalone import env_llm_port
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.core import diff as core_diff
from app.core import drift as core_drift
from app.core import importer as core_importer
from app.core import risk as core_risk
from app.core import schema_json as core_sj
from app.core import state_machine as core_sm
from app.core import suggest as core_suggest
from app.core import validation as core_validation
from app.core.design_session import (
    GateBlockedError,
    InvalidTransitionError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
)
from app.core.schema_json import SchemaJson, StateMachine
from app.core.sql_emitter import SUPPORTED_DRIVERS
from app.core.type_system import DEFAULT_REGISTRY
from app.designer import SchemaDesigner
from app.exporters import EXPORTERS, export_one
from app.generators import (
    export_framework_schema,
    generate_crud,
    generate_model,
    supported_frameworks,
)
from app.parsers import parse_import
from app.presets import field_presets
from app.schema_model import DatabaseSchema
from app.validators import SchemaValidator
from app.versioning import compare_schemas, diff_to_migration

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# In-memory design-session store (Milestone 1). Module-level so it survives across requests within a
# process; the production deployment would swap this for a persistent store behind the same API.
_SESSIONS = SessionStore()


def _env_llm() -> LLMClient | None:
    port = env_llm_port()
    return LLMClient(port) if port is not None else None


def _session_error(exc: Exception) -> JSONResponse:
    """Map design-session domain errors to HTTP responses (spec §6/§8)."""
    if isinstance(exc, SessionNotFoundError):
        return JSONResponse({"error": "session_not_found"}, status_code=404)
    if isinstance(exc, GateBlockedError):
        return JSONResponse(
            {"error": "gate_blocked", "reason": exc.reason, "blocking": exc.blocking}, status_code=409
        )
    if isinstance(exc, InvalidTransitionError):
        return JSONResponse({"error": exc.message, "state": exc.state}, status_code=409)
    raise exc  # pragma: no cover - unexpected errors propagate to the framework handler


def _coerce_schema(payload: dict) -> DatabaseSchema:
    """Accept either a bare schema dict or the module-output shape ({tables, driver, ...}).

    Reusable enums are materialized so the validator/exporters stay enum-agnostic.
    """
    data = payload.get("schema", payload) if isinstance(payload, dict) else {}
    return DatabaseSchema.model_validate(data).materialize_enums()


def register_interactive_routes(app: FastAPI) -> None:
    @app.post("/design")
    async def design_endpoint(request: dict) -> JSONResponse:
        feature_request = request.get("feature_request", "")
        settings = request.get("settings", {}) or {}
        designer = SchemaDesigner(_env_llm(), settings=settings)
        result = await designer.design(
            feature_request,
            existing_database=request.get("existing_database"),
        )
        return JSONResponse({"database_schema": result.model_dump(mode="json")})

    @app.post("/validate")
    async def validate_endpoint(request: dict) -> JSONResponse:
        schema = _coerce_schema(request)
        return JSONResponse({"validation": SchemaValidator(schema).validate()})

    @app.post("/export")
    async def export_endpoint(request: dict) -> JSONResponse:
        schema = _coerce_schema(request)
        export_type = request.get("type", "sql")
        try:
            # Canonical artifacts first (sql/migration/prisma/mermaid/openapi), then
            # framework full-schema exporters (django/sqlalchemy/typeorm/sequelize).
            if export_type in EXPORTERS:
                content = export_one(schema, export_type)
            else:
                content = export_framework_schema(schema, export_type)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"export_type": export_type, "content": content})

    @app.post("/generate/model")
    async def generate_model_endpoint(request: dict) -> JSONResponse:
        schema = _coerce_schema(request)
        framework = request.get("framework", "laravel")
        try:
            content = generate_model(schema, framework, request.get("table"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"framework": framework, "table": request.get("table"), "content": content})

    @app.post("/generate/crud")
    async def generate_crud_endpoint(request: dict) -> JSONResponse:
        schema = _coerce_schema(request)
        framework = request.get("framework", "laravel")
        try:
            content = generate_crud(schema, framework, request.get("table"), request.get("methods"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"framework": framework, "table": request.get("table"), "content": content})

    @app.get("/frameworks")
    async def frameworks_endpoint() -> JSONResponse:
        return JSONResponse(supported_frameworks())

    @app.get("/field-presets")
    async def field_presets_endpoint() -> JSONResponse:
        return JSONResponse({"presets": field_presets()})

    @app.post("/compare")
    async def compare_endpoint(request: dict) -> JSONResponse:
        old = DatabaseSchema.model_validate(request.get("old") or {}).materialize_enums()
        new = DatabaseSchema.model_validate(request.get("new") or {}).materialize_enums()
        diff = compare_schemas(old, new)
        return JSONResponse({"diff": diff, "migration": diff_to_migration(old, new, diff)})

    @app.post("/import")
    async def import_endpoint(request: dict) -> JSONResponse:
        import_type = request.get("type", "sql")
        data = request.get("data", "")
        try:
            schema = parse_import(import_type, data)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        validation = SchemaValidator(schema).validate()
        body = schema.model_dump(mode="json")
        body["validation"] = validation
        return JSONResponse({"database_schema": body})

    # ---------------------------------------------------------------------------------------------
    # Core (deterministic, layered schema_json) endpoints — thin wrappers over app.core (AD-4).
    # These speak the production-grade `schema_json` format from docs/ (Stable IDs + layers), as
    # opposed to the legacy /validate /export designer endpoints above which use the simpler model.
    # ---------------------------------------------------------------------------------------------
    def _load_core(payload: dict, key: str = "schema") -> SchemaJson:
        data = payload.get(key, payload) if isinstance(payload, dict) else {}
        return core_sj.load(data, validate=False)  # analysis tolerates structurally-imperfect input

    @app.post("/core/migrate")
    async def core_migrate(request: dict) -> JSONResponse:
        data = request.get("schema", request)
        migrated = core_sj.migrate(data)
        return JSONResponse({
            "formatVersion": migrated.get("formatVersion"),
            "structuralErrors": core_sj.validate_structure(migrated),
            "schema": migrated,
        })

    @app.post("/core/validate")
    async def core_validate(request: dict) -> JSONResponse:
        data = core_sj.migrate(request.get("schema", request))
        structural = core_sj.validate_structure(data)
        report = core_validation.validate(core_sj.load(data, validate=False))
        body = {"structuralErrors": structural, "report": report.model_dump()}
        if request.get("sarif"):
            body["sarif"] = report.to_sarif()
        return JSONResponse(body)

    @app.post("/core/diff")
    async def core_diff_endpoint(request: dict) -> JSONResponse:
        from_s = _load_core(request, "from")
        to_s = _load_core(request, "to")
        result = core_diff.diff(from_s, to_s)
        body = {
            "operations": result.op_dicts(),
            "changelog": result.changelog,
            "stats": result.stats,
            "colored": result.colored,
            "notes": result.notes,
        }
        if request.get("base") is not None:  # three-way (branching) conflict detection
            base = _load_core(request, "base")
            body["threeWay"] = core_diff.three_way_diff(base, from_s, to_s).model_dump()
        return JSONResponse(body)

    @app.post("/core/risk")
    async def core_risk_endpoint(request: dict) -> JSONResponse:
        driver = request.get("driver", "postgres")
        deploy_mode = request.get("deployMode", "rolling")
        if request.get("operations") is not None:
            operations = request["operations"]
        else:
            from_s = _load_core(request, "from")
            to_s = _load_core(request, "to")
            driver = request.get("driver") or core_diff._driver(to_s)
            operations = core_diff.diff(from_s, to_s).op_dicts()
        report = core_risk.analyze(operations, driver=driver, deploy_mode=deploy_mode)
        body = report.model_dump()
        body["checklist"] = report.checklist()
        if request.get("sarif"):
            body["sarif"] = report.to_sarif()
        return JSONResponse(body)

    @app.post("/core/state-machine")
    async def core_state_machine(request: dict) -> JSONResponse:
        raw = request.get("stateMachine") or request.get("state_machine")
        if not raw:
            return JSONResponse({"error": "missing stateMachine"}, status_code=400)
        try:
            sm = StateMachine.model_validate(raw)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        schema = _load_core(request) if request.get("schema") else None
        return JSONResponse(core_sm.derive_all(sm, schema))

    @app.get("/core/types")
    async def core_types() -> JSONResponse:
        return JSONResponse({
            "types": [
                {
                    "id": tid,
                    "category": DEFAULT_REGISTRY.get(tid).category,
                    "physical": DEFAULT_REGISTRY.get(tid).physical_default.model_dump(exclude_none=True),
                    "pii": DEFAULT_REGISTRY.get(tid).pii,
                }
                for tid in DEFAULT_REGISTRY.ids()
            ]
        })

    # ---------------------------------------------------------------------------------------------
    # Design Session orchestration (/design/*) — the Milestone 1 greenfield pipeline + approval
    # gate. Thin wrappers over app.core.design_session (AD-4): create a session, let AI suggest a
    # schema, a human applies + validates + submits + approves it, then migration/handoff become
    # available *only* once approved.
    # ---------------------------------------------------------------------------------------------
    def _import_db(dsn: str, name: str = "imported") -> dict:
        """Introspect a live Postgres database into an import result (Milestone 2 §1)."""
        introspected = core_importer.introspect_postgres(dsn)
        return core_importer.build_schema_json(introspected, name=name)

    def _introspect_schema(dsn: str, name: str) -> SchemaJson:
        return core_sj.load(_import_db(dsn, name)["schema_json"], validate=False)

    @app.post("/design/sessions")
    async def design_create(request: dict) -> JSONResponse:
        mode = request.get("mode", "greenfield")
        # Brownfield (spec M2 §0): the imported live schema becomes BOTH the initial draft and the
        # migration baseline, so editing + migration is a real delta from the database. The schema can
        # come from a live DSN (we introspect it) or be supplied pre-imported by the caller.
        if mode == "brownfield":
            imported = request.get("schema_json")
            dsn = request.get("importDsn") or request.get("import_dsn")
            if imported is None and dsn:
                try:
                    imported = _import_db(dsn, request.get("name", "imported"))["schema_json"]
                except Exception as exc:  # noqa: BLE001 - surface DB/driver problems as 400, not 500
                    return JSONResponse({"error": "import_failed", "detail": str(exc)}, status_code=400)
            if imported is None:
                return JSONResponse({"error": "brownfield requires importDsn or schema_json"}, status_code=400)
            session = _SESSIONS.create(mode="brownfield", schema_json=imported,
                                       baseline=imported, baseline_source="import")
            return JSONResponse(session.to_response())
        session = _SESSIONS.create(
            mode=mode,
            prd=request.get("prd"),
            schema_json=request.get("schema_json"),  # optional: user supplies a schema directly (no-LLM)
        )
        return JSONResponse(session.to_response())

    @app.post("/design/sessions/{session_id}/suggest")
    async def design_suggest(session_id: str, request: dict | None = None) -> JSONResponse:
        try:
            session = _SESSIONS.get(session_id)
            result = await core_suggest.suggest_schema(session.prd or "", llm=_env_llm())
            _SESSIONS.attach_suggestion(session_id, result["suggestion"])
        except (SessionNotFoundError, GateBlockedError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse(result)

    @app.post("/design/sessions/{session_id}/apply-suggestion")
    async def design_apply(session_id: str, request: dict) -> JSONResponse:
        schema = request.get("schema_json")
        if schema is None:
            return JSONResponse({"error": "missing schema_json"}, status_code=400)
        try:
            session = _SESSIONS.apply_schema(session_id, schema)
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse({"state": session.state, "schema_json": session.schema_doc})

    @app.post("/design/sessions/{session_id}/validate")
    async def design_validate(session_id: str) -> JSONResponse:
        try:
            session, outcome = _SESSIONS.validate(session_id)
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse({
            "state": session.state,
            "report": {
                "sarif": outcome.sarif,
                "summary": outcome.summary,
                "structuralErrors": outcome.structural_errors,
            },
        })

    @app.post("/design/sessions/{session_id}/submit")
    async def design_submit(session_id: str) -> JSONResponse:
        try:
            session = _SESSIONS.submit(session_id)
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse({"state": session.state})

    @app.post("/design/sessions/{session_id}/approve")
    async def design_approve(session_id: str, request: dict) -> JSONResponse:
        try:
            session = _SESSIONS.approve(
                session_id,
                approved_by=request.get("approvedBy") or request.get("approved_by") or "",
                acknowledge_critical=bool(request.get("acknowledgeCritical")),
            )
        except (SessionNotFoundError, GateBlockedError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse({
            "state": session.state,
            "schemaVersion": session.schema_version,
            "checksum": session.checksum,
        })

    @app.post("/design/sessions/{session_id}/reject")
    async def design_reject(session_id: str) -> JSONResponse:
        try:
            session = _SESSIONS.reject(session_id)
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse({"state": session.state})

    @app.post("/design/sessions/{session_id}/revise")
    async def design_revise(session_id: str) -> JSONResponse:
        try:
            session = _SESSIONS.revise(session_id)
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)
        return JSONResponse(session.to_response())

    @app.get("/design/sessions/{session_id}")
    async def design_get(session_id: str) -> JSONResponse:
        try:
            session = _SESSIONS.get(session_id)
        except SessionNotFoundError as exc:
            return _session_error(exc)
        return JSONResponse(session.to_response())

    @app.get("/design/sessions/{session_id}/migration")
    async def design_migration(session_id: str) -> JSONResponse:
        try:
            return JSONResponse(_SESSIONS.migration(session_id))
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)

    @app.get("/design/sessions/{session_id}/handoff")
    async def design_handoff(session_id: str) -> JSONResponse:
        try:
            return JSONResponse(_SESSIONS.handoff(session_id))
        except (SessionNotFoundError, InvalidTransitionError) as exc:
            return _session_error(exc)

    @app.post("/design/import")
    async def design_import(request: dict) -> JSONResponse:
        """Introspect a live Postgres database into a Stable-ID schema_json (Milestone 2 §1).

        Deterministic reverse-inference first; when an LLM is configured it only *enriches* the
        ambiguous columns (the result still carries them as suggestions for the human to confirm).
        """
        dsn = request.get("dsn")
        if not dsn:
            return JSONResponse({"error": "missing dsn"}, status_code=400)
        try:
            result = _import_db(dsn, request.get("name", "imported"))
        except Exception as exc:  # noqa: BLE001 - DB/driver errors are client-fixable → 400
            return JSONResponse({"error": "import_failed", "detail": str(exc)}, status_code=400)
        if request.get("enrich"):
            result = await core_importer.enrich_ambiguous(result, _env_llm())
        return JSONResponse(result)

    @app.post("/design/drift")
    async def design_drift(request: dict) -> JSONResponse:
        """Three-way drift: Designed ↔ Migrations ↔ Live (Milestone 2 §2). Report-only (AD-5).

        Each leg may be supplied inline (a schema_json) or sourced from a database: ``liveDsn`` is
        introspected directly (Leg C); ``migrationsDsn`` is an already-prepared shadow DB, or
        ``migrationsDir`` + ``shadowDsn`` applies the raw-SQL files to a shadow DB then introspects it.
        """
        designed_raw = request.get("designed")
        if designed_raw is None:
            return JSONResponse({"error": "missing designed schema_json"}, status_code=400)
        try:
            designed = core_sj.load(designed_raw, validate=False)
            live = _resolve_leg(request, "live", "liveDsn")
            migrations = _resolve_migrations_leg(request)
        except Exception as exc:  # noqa: BLE001 - DB/driver/parse errors → 400
            return JSONResponse({"error": "drift_failed", "detail": str(exc)}, status_code=400)
        report = core_drift.three_way_drift(designed, migrations, live)
        body: dict = {
            "reconcile": report.reconcile.model_dump(),
            "drift": [d.model_dump() for d in report.drift],
            "summary": report.summary,
            "exitCode": report.exit_code,
        }
        if request.get("sarif"):
            body["sarif"] = report.to_sarif()
        return JSONResponse(body)

    def _resolve_leg(request: dict, inline_key: str, dsn_key: str) -> SchemaJson | None:
        if request.get(inline_key) is not None:
            return core_sj.load(request[inline_key], validate=False)
        if request.get(dsn_key):
            return _introspect_schema(request[dsn_key], inline_key)
        return None

    def _resolve_migrations_leg(request: dict) -> SchemaJson | None:
        if request.get("migrations") is not None:
            return core_sj.load(request["migrations"], validate=False)
        if request.get("migrationsDsn"):
            return _introspect_schema(request["migrationsDsn"], "migrations")
        # migrationsDir + shadowDsn: apply the raw-SQL files to a shadow DB, then introspect it.
        migrations_dir = request.get("migrationsDir")
        shadow_dsn = request.get("shadowDsn")
        if migrations_dir and shadow_dsn:
            files = sorted(Path(migrations_dir).glob("*.sql"))
            statements: list[str] = []
            for f in files:
                statements += core_importer.split_sql(f.read_text(encoding="utf-8"))
            core_importer.apply_sql(shadow_dsn, statements)
            return _introspect_schema(shadow_dsn, "migrations")
        return None

    @app.get("/capabilities")
    async def capabilities() -> JSONResponse:
        """Capability manifest for the Designer expert module (spec §3)."""
        return JSONResponse({
            "module": "visual_database_designer",
            "milestone": "m2-brownfield",
            "modes": ["greenfield", "brownfield"],
            "drivers": list(SUPPORTED_DRIVERS),
            "sessionStates": [s.value for s in SessionState],
            "baselineSources": ["empty", "approved", "import"],
            "endpoints": {
                "core": ["/core/migrate", "/core/validate", "/core/diff", "/core/risk",
                         "/core/state-machine", "/core/types"],
                "design": ["/design/sessions", "/design/sessions/{id}/suggest",
                           "/design/sessions/{id}/apply-suggestion", "/design/sessions/{id}/validate",
                           "/design/sessions/{id}/submit", "/design/sessions/{id}/approve",
                           "/design/sessions/{id}/reject", "/design/sessions/{id}/revise",
                           "/design/sessions/{id}/migration", "/design/sessions/{id}/handoff"],
                "brownfield": ["/design/import", "/design/sessions (mode=brownfield)", "/design/drift"],
            },
            "driftCategories": ["synced", "migration_not_applied", "manual_prod_change",
                                "design_ahead_of_code", "code_ahead_of_design", "migration_incomplete"],
            "guarantees": {
                "deterministic": "validate/diff/risk/sql AND import are byte-identical for the same input",
                "approvalGate": "migration & handoff require state=approved",
                "aiBoundary": "the LLM only suggests/enriches; the rest of the pipeline is LLM-free",
                "driftSafety": "drift is report-only; every reconciliation is a human-approved suggestion",
            },
        })

    @app.get("/canvas")
    async def canvas_page():
        index = _FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return PlainTextResponse("Canvas frontend is not bundled in this build.", status_code=404)

    # Serve the canvas assets (canvas.js, styles.css, components/*) when present.
    if _FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")
