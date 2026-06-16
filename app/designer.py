"""SchemaDesigner — the engine that ties the stages together (tasks 6F.3-6F.5, 6F.7).

  design()  : feature_request → AI/heuristic schema → validate → export → improve → result
  validate(): an existing schema → validation report
  export()  : an existing schema → one artifact

It depends only on the injected `llm` client and a plain dict input, so it can be driven directly
from a unit test with a simple mock (or no LLM at all — the template/heuristic layers still produce
a valid, exportable schema).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.exporters import export_all
from app.output import DatabaseSchemaResult, Exports, Validation
from app.parsers import parse_import
from app.schema_model import DatabaseSchema
from app.suggestions import SchemaSuggestions
from app.validators import SchemaValidator

if TYPE_CHECKING:
    from aiarch_module_sdk import LLMClient

_DEFAULT_DRIVER = {"sql": "postgresql", "nosql": "mongodb", "vector": "postgresql"}


class SchemaDesigner:
    def __init__(self, llm_client: LLMClient | None, *, settings: dict | None = None):
        self.llm = llm_client
        self.settings = settings or {}
        self.suggestions = SchemaSuggestions(llm_client)

    async def design(
        self, feature_request: str, *, existing_database: str | None = None, ctx=None
    ) -> DatabaseSchemaResult:
        db_type = str(self.settings.get("database_type", "sql"))
        driver = str(self.settings.get("driver") or _DEFAULT_DRIVER.get(db_type, "postgresql"))

        if existing_database:
            # Brownfield: import an existing schema, then enrich with suggestions.
            schema = parse_import(self.settings.get("import_type", "sql"), existing_database)
            schema.driver = driver
        else:
            schema = await self.suggestions.suggest_schema(feature_request, driver=driver, ctx=ctx)

        schema.type = db_type
        schema.materialize_enums()  # resolve enum_ref → field.values before validate/export
        result = self._assemble(schema)

        if self.settings.get("ai_suggestions", True):
            result.suggestions = await self.suggestions.suggest_improvements(schema, ctx=ctx)

        if ctx is not None:
            ctx.log(
                f"designed schema '{schema.id}': {len(schema.tables)} table(s), "
                f"valid={result.validation.valid}, {len(result.suggestions)} suggestion(s)"
            )
        return result

    def validate(self, schema: DatabaseSchema) -> dict:
        return SchemaValidator(schema).validate()

    def _assemble(self, schema: DatabaseSchema) -> DatabaseSchemaResult:
        validation = SchemaValidator(schema).validate()
        exports = export_all(schema)
        return DatabaseSchemaResult(
            id=schema.id,
            version=schema.version,
            type=schema.type,
            driver=schema.driver,
            tables=schema.tables,
            relations=schema.all_relations(),
            validation=Validation(**validation),
            exports=Exports(**exports),
        )
