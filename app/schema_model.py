"""Internal schema representation (task 6F.2).

The doc sketches these as dataclasses; we use Pydantic models instead so the schema round-trips
through the Module Protocol envelope (JSON in → validated objects → JSON out) for free and so a
partial/LLM-generated payload still validates with sensible defaults.

`SchemaField` / `Relation` / `Table` / `DatabaseSchema` are the canonical objects every other
stage (validator, exporters, parsers, comparator, suggestions) operates on.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FieldType(StrEnum):
    # SQL
    BIGINT = "bigint"
    INTEGER = "integer"
    VARCHAR = "varchar"
    TEXT = "text"
    BOOLEAN = "boolean"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    TIMESTAMP = "timestamp"
    JSON = "json"
    UUID = "uuid"
    ENUM = "enum"
    FOREIGN_ID = "foreign_id"

    # NoSQL
    STRING = "string"
    NUMBER = "number"
    ARRAY = "array"
    OBJECT = "object"

    # Vector
    VECTOR = "vector"
    EMBEDDING = "embedding"

    @classmethod
    def coerce(cls, value: object) -> FieldType:
        """Best-effort map an arbitrary string to a FieldType (defaults to VARCHAR)."""
        if isinstance(value, FieldType):
            return value
        text = str(value or "").strip().lower()
        try:
            return cls(text)
        except ValueError:
            return _TYPE_ALIASES.get(text, FieldType.VARCHAR)


# Common aliases coming from SQL dumps / LLM output → our canonical FieldType.
_TYPE_ALIASES: dict[str, FieldType] = {
    "int": FieldType.INTEGER,
    "int4": FieldType.INTEGER,
    "int8": FieldType.BIGINT,
    "bigserial": FieldType.BIGINT,
    "serial": FieldType.INTEGER,
    "smallint": FieldType.INTEGER,
    "char": FieldType.VARCHAR,
    "character varying": FieldType.VARCHAR,
    "string": FieldType.VARCHAR,
    "longtext": FieldType.TEXT,
    "mediumtext": FieldType.TEXT,
    "bool": FieldType.BOOLEAN,
    "tinyint": FieldType.BOOLEAN,
    "float": FieldType.DECIMAL,
    "double": FieldType.DECIMAL,
    "numeric": FieldType.DECIMAL,
    "real": FieldType.DECIMAL,
    "datetime": FieldType.DATETIME,
    "timestamptz": FieldType.TIMESTAMP,
    "jsonb": FieldType.JSON,
    "uuid": FieldType.UUID,
}


class RelationType(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"
    POLYMORPHIC = "polymorphic"


class SchemaField(BaseModel):
    name: str
    type: FieldType = FieldType.VARCHAR
    nullable: bool = True
    unique: bool = False
    indexed: bool = False
    primary_key: bool = False
    auto_increment: bool = False
    default: str | int | float | bool | None = None
    length: int | None = None  # varchar
    precision: int | None = None  # decimal
    scale: int | None = None
    values: list[str] | None = None  # enum (inline)
    enum_ref: str | None = None  # name of a schema-level reusable enum (Phase 3 #13)
    description: str | None = None  # column comment (Phase 3 #16)


class EnumDef(BaseModel):
    """A reusable, named enum referenced by fields via `enum_ref` (Phase 3 #13)."""

    name: str
    values: list[str] = Field(default_factory=list)
    description: str | None = None


class Index(BaseModel):
    """An explicit (possibly composite) index on a table (Phase 3 #15)."""

    name: str = ""
    columns: list[str] = Field(default_factory=list)
    unique: bool = False
    type: str = "btree"  # btree | fulltext

    def resolved_name(self, table: str) -> str:
        return self.name or f"{table}_{'_'.join(self.columns)}_{'uq' if self.unique else 'idx'}"


class Relation(BaseModel):
    name: str = ""
    from_table: str
    from_field: str = ""
    to_table: str
    to_field: str = "id"
    type: RelationType = RelationType.ONE_TO_MANY
    on_delete: str = "cascade"  # cascade | restrict | set_null | no_action
    on_update: str = "cascade"
    description: str | None = None


class Table(BaseModel):
    name: str
    fields: list[SchemaField] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)  # explicit/composite indexes (Phase 3 #15)
    soft_delete: bool = False
    timestamps: bool = True
    group: str | None = None  # optional domain/group label for canvas color-coding (Phase 2)
    description: str | None = None  # table comment (Phase 3 #16)

    def field(self, name: str) -> SchemaField | None:
        return next((f for f in self.fields if f.name == name), None)

    def primary_keys(self) -> list[str]:
        return [f.name for f in self.fields if f.primary_key]


class DatabaseSchema(BaseModel):
    id: str = "schema"
    type: str = "sql"  # sql | nosql | vector
    driver: str = "postgresql"  # postgresql | mysql | mongodb | sqlite
    version: int = 1
    tables: list[Table] = Field(default_factory=list)
    enums: list[EnumDef] = Field(default_factory=list)  # reusable named enums (Phase 3 #13)
    metadata: dict = Field(default_factory=dict)

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    def enum(self, name: str) -> EnumDef | None:
        return next((en for en in self.enums if en.name == name), None)

    def all_relations(self) -> list[Relation]:
        """Flatten every table's relations into one list (top-level `relations` for the output)."""
        out: list[Relation] = []
        for t in self.tables:
            out.extend(t.relations)
        return out

    def materialize_enums(self) -> DatabaseSchema:
        """Copy each named enum's values into the fields that reference it via `enum_ref`.

        Lets the exporters/validator stay enum-agnostic (they only look at `field.values`), while the
        canvas defines enums once and references them. Mutates in place and returns self.
        """
        for table in self.tables:
            for field in table.fields:
                if field.enum_ref and not field.values:
                    en = self.enum(field.enum_ref)
                    if en:
                        field.type = FieldType.ENUM
                        field.values = list(en.values)
        return self
