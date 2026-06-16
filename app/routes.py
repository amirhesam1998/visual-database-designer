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


def _env_llm() -> LLMClient | None:
    port = env_llm_port()
    return LLMClient(port) if port is not None else None


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

    @app.get("/canvas")
    async def canvas_page():
        index = _FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return PlainTextResponse("Canvas frontend is not bundled in this build.", status_code=404)

    # Serve the canvas assets (canvas.js, styles.css, components/*) when present.
    if _FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")
