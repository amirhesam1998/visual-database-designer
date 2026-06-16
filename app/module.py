"""Visual Database Designer — the fourth expert module (Phase 6F).

A both-modes **design** module: it consumes a free-text `feature_request` (greenfield) or an
existing database dump (brownfield), and emits a `database_schema` — the internal table/relation
model plus a validation report and ready-to-use exports (SQL, Laravel migration, Prisma, Mermaid
ERD, OpenAPI stub). With an LLM it designs the schema and suggests improvements; with no LLM it
falls back to deterministic, domain-aware templates so it stays fully functional offline.

NOTE: the Module Protocol's manifest `type` is one of analysis|generation|review|input — this is a
schema *generator*, so it declares `type="generation"` even though we call it a "design" module
conceptually.

The `feature_request` reaches the module either as an explicit input (standalone) or via
`ctx.settings["feature_request"]` (the platform threads the brownfield project settings through —
the same path the Feature Implementation module uses).
"""

from __future__ import annotations

from aiarch_module_sdk import Manifest, Module, Needs, RunContext, build_module_app

from app.designer import SchemaDesigner
from app.output import DatabaseSchemaResult
from app.routes import register_interactive_routes


class VisualDatabaseDesignerModule(Module):
    manifest = Manifest(
        name="visual_database_designer",
        version="0.1.0",
        protocol_version="1.0",
        type="generation",
        consumes=[],
        produces="database_schema",
        modes=["greenfield", "brownfield"],
        required=False,
        parallel_safe=True,
        needs=Needs(llm=True, source=False, knowledge=False),
        title="Visual Database Designer",
        description="Designs a normalized database schema from a feature description (or imports an existing one), "
        "validates it, and exports SQL, Laravel migrations, Prisma, a Mermaid ERD and an OpenAPI stub.",
    )
    output_model = DatabaseSchemaResult

    async def run(self, inputs: dict, ctx: RunContext) -> dict:
        feature_request = (
            inputs.get("feature_request")
            or ctx.settings.get("feature_request")
            or ctx.settings.get("raw_idea")
            or ""
        )
        existing_database = inputs.get("existing_database") or ctx.settings.get("existing_database")

        designer = SchemaDesigner(ctx.llm, settings=ctx.settings)
        result = await designer.design(feature_request, existing_database=existing_database, ctx=ctx)

        ctx.log(
            f"database_schema ready: {len(result.tables)} table(s), "
            f"valid={result.validation.valid}, {len(result.validation.warnings)} warning(s)"
        )
        return result.model_dump()

    async def health(self) -> dict:
        return {"status": "ok", "module": self.manifest.name, "version": self.manifest.version}


app = build_module_app(VisualDatabaseDesignerModule())
register_interactive_routes(app)
