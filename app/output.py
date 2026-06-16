"""Output schema for the Visual Database Designer — the `database_schema` payload.

Mirrors the contract in Docs/PHASE-6F-VISUAL-DATABASE-DESIGNER.md (§1 Output). Every field carries a
default so a partial run (e.g. LLM unavailable) still serialises a schema-valid payload instead of
failing the run.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schema_model import Relation, Table


class Validation(BaseModel):
    valid: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Exports(BaseModel):
    sql: str = ""
    migration: str = ""
    prisma: str = ""
    mermaid: str = ""
    openapi: str = ""
    markdown: str = ""


class DatabaseSchemaResult(BaseModel):
    id: str = "schema"
    version: int = 1
    type: str = "sql"
    driver: str = "postgresql"
    tables: list[Table] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    validation: Validation = Field(default_factory=Validation)
    exports: Exports = Field(default_factory=Exports)
    suggestions: list[str] = Field(default_factory=list)
