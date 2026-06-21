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

These are additive and standalone-friendly: /design synthesizes an LLM client from the module's own
environment (falling back to the offline heuristic when no LLM is configured).

The interactive visual designer is the built React SPA served at ``/designer`` (``frontend-canvas``).
The original no-build ``/canvas`` SPA has been removed — ``/designer`` is now the sole UI reference,
and these REST endpoints remain as the module's stable, framework-agnostic API surface.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiarch_module_sdk import LLMClient
from aiarch_module_sdk.standalone import env_llm_port
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core import api_client as core_api_client
from app.core import api_contract as core_api_contract
from app.core import api_server as core_api_server
from app.core import codegen_bridge as core_codegen
from app.core import diff as core_diff
from app.core import drift as core_drift
from app.core import e2e as core_e2e
from app.core import importer as core_importer
from app.core import risk as core_risk
from app.core import schema_json as core_sj
from app.core import seeder as core_seeder
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
from app.core.sql_emitter import SUPPORTED_DRIVERS, emit_sql
from app.core.type_system import DEFAULT_REGISTRY, resolve_fk_physical
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

_CANVAS_DIST = Path(__file__).resolve().parent.parent / "frontend-canvas" / "dist"

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

    # ---------------------------------------------------------------------------------------------
    # Canvas render projection (Canvas Milestone 1, read-only) — a thin, deterministic *view* over a
    # schema_json for the visual designer. It computes NOTHING new: it reuses the Type System's
    # existing resolution (``DEFAULT_REGISTRY.resolve`` + ``resolve_fk_physical``) so the canvas never
    # re-derives database logic in the front-end (spec §0/§7) and a foreign-key column shows its
    # referenced primary key's physical type (a uuid FK renders as ``uuid``, never an integer — the
    # lesson of the whole project). Read-only: it never mutates state or the schema.
    # ---------------------------------------------------------------------------------------------
    def _physical_label(physical: dict) -> str:
        base = physical.get("type", "")
        if physical.get("length"):
            return f"{base}({physical['length']})"
        if physical.get("precision") is not None:
            scale = physical.get("scale", 0)
            return f"{base}({physical['precision']},{scale})"
        if physical.get("dimension"):
            return f"{base}({physical['dimension']})"
        return base

    def _render_model(schema: SchemaJson) -> dict:
        # FK fields inherit the referenced PK's physical type (one shared resolution).
        fk_physical = resolve_fk_physical(schema)
        fk_field_ids = {
            rel.foreign_key_field_id
            for rel in schema.logical.relations
            if rel.foreign_key_field_id
        }
        tables = []
        for table in schema.logical.tables:
            fields = []
            for field in table.fields:
                physical: dict = {}
                pii = False
                sensitivity = None
                try:
                    resolved = DEFAULT_REGISTRY.resolve(field)
                    physical = dict(resolved.physical)
                    pii = bool(resolved.privacy.get("pii"))
                    sensitivity = resolved.privacy.get("sensitivity")
                except KeyError:
                    physical = {"type": field.semantic_type}  # unregistered type → show as-is
                if field.id in fk_physical:  # FK column takes the referenced PK's physical type
                    physical = dict(fk_physical[field.id])
                fields.append({
                    "id": field.id,
                    "name": field.name,
                    "semanticType": field.semantic_type,
                    "physicalType": _physical_label(physical),
                    "nullable": field.nullable,
                    "isPrimaryKey": field.is_primary_key,
                    "isForeignKey": field.id in fk_field_ids,
                    "pii": pii,
                    "sensitivity": sensitivity,
                    "enumId": field.enum_id,
                    "comment": field.comment,
                })
            tables.append({
                "id": table.id,
                "name": table.name,
                "comment": table.comment,
                "kind": table.kind,
                "fields": fields,
            })
        relations = [
            {
                "id": rel.id,
                "type": rel.type,
                "fromTableId": rel.from_table_id,
                "toTableId": rel.to_table_id,
                "foreignKeyFieldId": rel.foreign_key_field_id,
                "onDelete": rel.on_delete,
                "onUpdate": rel.on_update,
            }
            for rel in schema.logical.relations
        ]
        nodes = []
        if schema.presentation is not None:
            nodes = [
                {"tableId": n.table_id, "x": n.x, "y": n.y, "color": n.color, "group": n.group}
                for n in schema.presentation.nodes
            ]
        enums = [
            {"id": e.id, "name": e.name, "values": [v.value for v in e.values]}
            for e in schema.logical.enums
        ]
        return {
            "meta": (schema.meta.model_dump(by_alias=True, exclude_none=True) if schema.meta else {}),
            "tables": tables,
            "relations": relations,
            "enums": enums,
            "presentation": {"nodes": nodes},
            "hasLayout": len(nodes) > 0,
        }

    @app.post("/design/render")
    async def design_render(request: dict) -> JSONResponse:
        """Read-only render projection for the visual canvas (Canvas M1).

        Accepts a ``schema_json`` (or ``handoff`` with one), or a ``sessionId`` to render the current
        draft. Returns resolved-type tables, directional relations and presentation positions — no
        editing, no engine logic in the caller.
        """
        if request.get("sessionId") or request.get("session_id"):
            sid = request.get("sessionId") or request.get("session_id")
            try:
                session = _SESSIONS.get(sid)
            except SessionNotFoundError as exc:
                return _session_error(exc)
            schema = core_sj.load(session.schema_doc, validate=False)
        else:
            payload = request.get("schema_json") or request.get("handoff", {}).get("schema_json") \
                if isinstance(request.get("handoff"), dict) else request.get("schema_json")
            if payload is None:
                payload = request.get("schema") or request
            try:
                schema = core_sj.load(core_sj.migrate(payload), validate=False)
            except Exception as exc:  # noqa: BLE001 - malformed input → 400, not 500
                return JSONResponse({"error": "invalid_schema_json", "detail": str(exc)}, status_code=400)
        return JSONResponse(_render_model(schema))

    @app.post("/design/presentation")
    async def design_presentation(request: dict) -> JSONResponse:
        """Save canvas layout into the ``presentation`` layer (Canvas M2 §4).

        Layout is display-only and NOT schema-affecting (the diff engine ignores ``presentation``),
        so this is intentionally separate from the edit/validate cycle — moving a table never counts
        as a schema change. With a ``sessionId`` the layout is persisted onto the session's draft
        without altering its state; otherwise the supplied ``schema_json`` is echoed back with the
        layout merged in (stateless mode, used by the sample/standalone canvas). This is the single
        thin endpoint the spec permits for presentation (spec §8).
        """
        nodes = request.get("nodes")
        if nodes is None:
            return JSONResponse({"error": "missing nodes"}, status_code=400)
        sid = request.get("sessionId") or request.get("session_id")
        if sid:
            try:
                session = _SESSIONS.update_presentation(sid, nodes)
            except SessionNotFoundError as exc:
                return _session_error(exc)
            return JSONResponse({"schema_json": session.schema_doc, "persisted": True})
        payload = request.get("schema_json") or request.get("schema")
        if payload is None:
            return JSONResponse({"error": "missing schema_json or sessionId"}, status_code=400)
        try:
            doc = core_sj.migrate(payload)
        except Exception as exc:  # noqa: BLE001 - malformed input → 400, not 500
            return JSONResponse({"error": "invalid_schema_json", "detail": str(exc)}, status_code=400)
        presentation = dict(doc.get("presentation") or {})
        presentation["nodes"] = nodes
        doc["presentation"] = presentation
        return JSONResponse({"schema_json": doc, "persisted": False})

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
        """Import an existing database into a Stable-ID schema_json (Milestone 2 §1 + file import).

        Two sources, both reusing the M2 importer so reverse-inference is identical (a uuid FK stays
        ``uuid``):

        * **live** — ``dsn`` introspects a running Postgres directly.
        * **file** — ``sql``/``ddl`` (a ``CREATE TABLE`` dump) is applied to a temporary *shadow*
          database, which is then introspected. The shadow DSN comes from the request (``shadowDsn``)
          or the ``VDB_SHADOW_DSN`` env var; the user's real database is never written to.

        Deterministic reverse-inference first; when an LLM is configured (``enrich``) it only refines
        the ambiguous columns (still carried as suggestions for the human to confirm — AD-5).
        """
        name = request.get("name", "imported")
        sql = request.get("sql") or request.get("ddl")
        try:
            if sql is not None:
                shadow_dsn = (
                    request.get("shadowDsn") or request.get("shadow_dsn") or os.getenv("VDB_SHADOW_DSN")
                )
                if not shadow_dsn:
                    return JSONResponse(
                        {"error": "shadow_db_unavailable",
                         "detail": "file import needs a shadow database; set VDB_SHADOW_DSN or pass shadowDsn"},
                        status_code=400,
                    )
                result = core_importer.import_sql_via_shadow(sql, shadow_dsn, name=name)
            else:
                dsn = request.get("dsn")
                if not dsn:
                    return JSONResponse({"error": "missing dsn or sql"}, status_code=400)
                result = _import_db(dsn, name)
        except Exception as exc:  # noqa: BLE001 - DB/driver/SQL errors are client-fixable → 400
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

    @app.post("/design/seed")
    async def design_seed(request: dict) -> JSONResponse:
        """Scenario-based seeder (Milestone 3): generate valid, insertable rows for a schema.

        Input is preferably an approved handoff (`{handoff:{schema_json}}`) but a raw `schema_json`
        works too. Deterministic for a given `seed`; the LLM (if configured) only enriches text.
        """
        schema = request.get("schema_json") or (request.get("handoff") or {}).get("schema_json")
        if schema is None:
            return JSONResponse({"error": "missing schema_json"}, status_code=400)
        try:
            result = core_seeder.seed_data(
                schema,
                seed=int(request.get("seed", core_seeder.DEFAULT_SEED)),
                scenario=request.get("scenario"),
                output=request.get("output", "sql"),
            )
        except core_seeder.SeedError as exc:
            return JSONResponse({"error": "unseedable", "detail": str(exc)}, status_code=422)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if request.get("enrich"):
            result = await core_seeder.enrich_text(result, _env_llm())
        return JSONResponse(result)

    @app.post("/design/run-e2e")
    async def design_run_e2e(request: dict) -> JSONResponse:
        """End-to-end integration: run the whole chain (suggest → approval → migration → seed → API) on
        a real Postgres and report each step. Pure orchestration over the existing subsystems — the
        approval gate is the only pause (set `autoApprove` for automated runs). Requires a real `dsn`.
        """
        dsn = request.get("dsn")
        if not dsn:
            return JSONResponse({"error": "missing dsn"}, status_code=400)
        mode = request.get("mode", "greenfield")
        if mode not in {"greenfield", "brownfield"}:
            return JSONResponse({"error": f"unsupported mode {mode!r}"}, status_code=400)
        try:
            result = await core_e2e.run_e2e(
                _SESSIONS,
                mode=mode,
                dsn=dsn,
                prd=request.get("prd"),
                schema_json=request.get("schema_json"),
                auto_approve=bool(request.get("autoApprove") or request.get("auto_approve")),
                seed=int(request.get("seed", core_seeder.DEFAULT_SEED)),
                scenario=request.get("scenario"),
                version=request.get("version", "v1"),
                llm=_env_llm(),
            )
        except Exception as exc:  # noqa: BLE001 - DB/driver setup errors are client-fixable → 400
            return JSONResponse({"error": "e2e_failed", "detail": str(exc)}, status_code=400)
        status = 200 if result.get("result") in {"green", "awaiting_approval"} else 422
        return JSONResponse(result, status_code=status)

    @app.post("/design/api/contract")
    async def design_api_contract(request: dict) -> JSONResponse:
        """Generate the OpenAPI 3.1 contract from a schema (Milestone 4) — the source of truth."""
        schema = request.get("schema_json") or (request.get("handoff") or {}).get("schema_json")
        if schema is None:
            return JSONResponse({"error": "missing schema_json"}, status_code=400)
        version = request.get("version", "v1")
        openapi = core_api_contract.build_openapi(schema, version=version)
        return JSONResponse({"openapi": openapi, "stats": core_api_contract.contract_stats(openapi)})

    @app.post("/design/api/server")
    async def design_api_server(request: dict) -> JSONResponse:
        """Generate the reference FastAPI server files (Milestone 4 §3)."""
        schema = request.get("schema_json") or (request.get("handoff") or {}).get("schema_json")
        if schema is None:
            return JSONResponse({"error": "missing schema_json"}, status_code=400)
        target = request.get("target", "fastapi")
        if target != "fastapi":
            return JSONResponse({"error": f"unsupported target {target!r}; Milestone 4 supports 'fastapi'"},
                                status_code=400)
        files = core_api_server.generate_server_files(schema, version=request.get("version", "v1"))
        return JSONResponse({"files": files, "target": target})

    @app.post("/design/api/client")
    async def design_api_client(request: dict) -> JSONResponse:
        """Generate a TypeScript client + Postman collection from the OpenAPI document (secondary)."""
        openapi = request.get("openapi")
        if openapi is None:
            schema = request.get("schema_json") or (request.get("handoff") or {}).get("schema_json")
            if schema is None:
                return JSONResponse({"error": "missing openapi or schema_json"}, status_code=400)
            openapi = core_api_contract.build_openapi(schema, version=request.get("version", "v1"))
        try:
            result = core_api_client.generate_client(openapi, target=request.get("target", "typescript"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result)

    @app.post("/design/code")
    async def design_code(request: dict) -> JSONResponse:
        """Generate code from a ``schema_json`` (unify spec phase 2 §2 — the code-gen bridge).

        ``kind`` selects the artifact. ``sql`` stays in the deterministic Core (diff-from-empty →
        SQL emitter); ``model``/``crud``/``schema`` reuse the proven legacy generators behind the
        ``schema_json → DatabaseSchema`` bridge (:mod:`app.core.codegen_bridge`). No generation logic
        is duplicated and ``schema_json`` remains the source of truth (no reverse translation).
        """
        payload = request.get("schema_json") or (request.get("handoff") or {}).get("schema_json")
        if payload is None:
            return JSONResponse({"error": "missing schema_json"}, status_code=400)
        kind = request.get("kind", "sql")
        try:
            schema = core_sj.load(core_sj.migrate(payload), validate=False)
        except Exception as exc:  # noqa: BLE001 - malformed input → 400, not 500
            return JSONResponse({"error": "invalid_schema_json", "detail": str(exc)}, status_code=400)
        try:
            if kind == "sql":
                driver = request.get("driver", "postgres")
                empty = core_sj.load({"formatVersion": "1.0.0", "logical": {"tables": []}}, validate=False)
                script = emit_sql(core_diff.diff(empty, schema).op_dicts(), schema, driver=driver)
                content = ";\n\n".join(script.up_statements())
                content = content + ";\n" if content else "-- (empty schema)\n"
                return JSONResponse({"kind": kind, "language": "sql", "content": content})
            legacy = core_codegen.to_legacy_schema(schema)
            if kind == "model":
                content = generate_model(legacy, request.get("framework", "laravel"), request.get("table"))
            elif kind == "crud":
                content = generate_crud(
                    legacy, request.get("framework", "laravel"), request.get("table"), request.get("methods")
                )
            elif kind == "schema":
                framework = request.get("framework", "prisma")
                content = export_one(legacy, framework) if framework in EXPORTERS \
                    else export_framework_schema(legacy, framework)
            else:
                return JSONResponse({"error": f"unsupported kind {kind!r}"}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"kind": kind, "framework": request.get("framework"), "content": content})

    @app.get("/design/code/frameworks")
    async def design_code_frameworks() -> JSONResponse:
        """Frameworks/targets the code-gen bridge offers (drives the Code panel's dropdowns)."""
        fw = supported_frameworks()
        return JSONResponse({
            "sql": ["postgres"],
            "model": fw["model"],
            "crud": fw["crud"],
            "crudMethods": fw["crud_methods"],
            # `schema` = whole-schema exporters faithful to schema_json (mermaid intentionally dropped).
            "schema": ["prisma", "markdown", "django", "sqlalchemy", "typeorm", "sequelize"],
        })

    @app.get("/capabilities")
    async def capabilities() -> JSONResponse:
        """Capability manifest for the Designer expert module (spec §3)."""
        return JSONResponse({
            "module": "visual_database_designer",
            "milestone": "e2e-integration",
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
                "brownfield": ["/design/import (live dsn)", "/design/import (sql file via shadow db)",
                               "/design/sessions (mode=brownfield)", "/design/drift"],
                "seeder": ["/design/seed"],
                "api": ["/design/api/contract", "/design/api/server", "/design/api/client"],
                "integration": ["/design/run-e2e"],
                "codegen": ["/design/code", "/design/code/frameworks"],
                # /designer is the single visual reference (the legacy /canvas SPA was removed). It is a
                # thin shell over these engine endpoints — it decides nothing in the browser.
                "designer": ["/designer", "/design/render", "/design/presentation", "/core/types",
                             "/core/validate", "/core/diff", "/core/risk", "/design/code",
                             "/design/import", "/design/drift"],
            },
            "driftCategories": ["synced", "migration_not_applied", "manual_prod_change",
                                "design_ahead_of_code", "code_ahead_of_design", "migration_incomplete"],
            "scenarios": ["ecommerce_medium", "multi_tenant", "ticketing"],
            "guarantees": {
                "deterministic": "validate/diff/risk/sql, import AND seed are byte-identical for the same input/seed",
                "approvalGate": "migration & handoff require state=approved",
                "aiBoundary": "the LLM only suggests/enriches; the rest of the pipeline is LLM-free",
                "driftSafety": "drift is report-only; every reconciliation is a human-approved suggestion",
                "seedSafety": "the seeder produces data/SQL; applying it is an explicit step (no auto-apply)",
                "apiContract": "OpenAPI 3.1 is the source of truth; server & client derive from it (no auto-deploy)",
                "endToEnd": "run-e2e chains the subsystems unchanged; each step's output is the next's input",
            },
        })

    # The visual designer: the built React Flow + Tailwind SPA, served at /designer/. It is the sole
    # UI reference (the legacy no-build /canvas SPA has been removed). It is only present when the
    # frontend has been built (`cd frontend-canvas && npm run build`), so its absence never breaks the
    # API service — every feature it offers is also reachable through the REST endpoints above.
    @app.get("/designer")
    async def designer_redirect():
        return RedirectResponse(url="/designer/")

    if _CANVAS_DIST.exists():
        app.mount("/designer", StaticFiles(directory=str(_CANVAS_DIST), html=True), name="designer")
