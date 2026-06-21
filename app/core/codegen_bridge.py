"""Code-generation bridge: ``schema_json`` → legacy ``DatabaseSchema`` (unify spec phase 2 §2 C1–C3/C9).

The deterministic Core speaks ``schema_json`` (Stable IDs, layered, semantic types). The proven
multi-framework code generators / exporters in :mod:`app.generators` and :mod:`app.exporters` speak
the older, name-based :class:`~app.schema_model.DatabaseSchema`. Rather than re-write that generation
logic against ``schema_json`` (a large surface, and the spec forbids inventing new generation), this
module is a **one-way translator**: it projects a ``schema_json`` document down to a legacy
``DatabaseSchema`` purely so the existing generators can run on it. It is *never* used in reverse —
``schema_json`` stays the single source of truth (spec §3).

The one invariant that must not be lost in translation (the lesson of the whole project): a
foreign-key column inherits the **referenced primary key's physical type**. We resolve every field
through the same Type System pipeline the SQL emitter and importer use (``DEFAULT_REGISTRY.resolve``
+ ``resolve_fk_physical``), so a uuid FK arrives as a uuid in the generated code, never an integer.
Semantic types (Money → decimal, Email → varchar, …) likewise resolve through that one registry.
"""

from __future__ import annotations

from typing import Any

from app.core import schema_json as core_sj
from app.core.schema_json import Field_, SchemaJson
from app.core.schema_json import Table as CoreTable
from app.core.type_system import (
    DEFAULT_REGISTRY,
    UnsupportedPhysicalTypeError,
    resolve_fk_physical,
)
from app.schema_model import (
    DatabaseSchema,
    EnumDef,
    FieldType,
    Index,
    Relation,
    RelationType,
    SchemaField,
    Table,
)

# Resolved *physical* base type → legacy FieldType. Driven by the physical layer (not the semantic
# name) so an FK that resolved to its referenced PK's type, or a `money` that resolved to `decimal`,
# maps correctly. Anything unknown falls back to VARCHAR (the legacy model's own default).
_PHYSICAL_TO_FIELDTYPE: dict[str, FieldType] = {
    "uuid": FieldType.UUID,
    "varchar": FieldType.VARCHAR,
    "char": FieldType.VARCHAR,
    "text": FieldType.TEXT,
    "boolean": FieldType.BOOLEAN,
    "integer": FieldType.INTEGER,
    "int": FieldType.INTEGER,
    "smallint": FieldType.INTEGER,
    "bigint": FieldType.BIGINT,
    "decimal": FieldType.DECIMAL,
    "numeric": FieldType.DECIMAL,
    "money": FieldType.DECIMAL,
    "float": FieldType.DECIMAL,
    "double": FieldType.DECIMAL,
    "real": FieldType.DECIMAL,
    "date": FieldType.DATE,
    "timestamp": FieldType.TIMESTAMP,
    "timestamptz": FieldType.TIMESTAMP,
    "datetime": FieldType.DATETIME,
    "json": FieldType.JSON,
    "jsonb": FieldType.JSON,
    "vector": FieldType.VECTOR,
}

_REL_TYPE: dict[str, RelationType] = {
    "one_to_one": RelationType.ONE_TO_ONE,
    "one_to_many": RelationType.ONE_TO_MANY,
    "many_to_one": RelationType.MANY_TO_ONE,
    "many_to_many": RelationType.MANY_TO_MANY,
    "polymorphic": RelationType.POLYMORPHIC,
}

_TIMESTAMP_NAMES = {"created_at", "updated_at"}


def _physical_for(field: Field_, fk_physical: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve a field's physical spec — FK fields take the referenced PK's resolved type."""
    if field.id in fk_physical:
        return fk_physical[field.id]
    try:
        return DEFAULT_REGISTRY.resolve(field).physical
    except (KeyError, UnsupportedPhysicalTypeError):
        return {"type": field.semantic_type}  # unregistered semantic type → carry the name through


def _field_to_legacy(
    field: Field_,
    fk_physical: dict[str, dict[str, Any]],
    fk_field_ids: set[str],
    enum_name: str | None,
) -> SchemaField:
    physical = _physical_for(field, fk_physical)
    base = str(physical.get("type", "")).lower()
    if enum_name is not None:
        ftype = FieldType.ENUM
    elif field.id in fk_field_ids:
        # A foreign-key column: keep the referenced PK's storage type (uuid stays uuid), but the
        # legacy model has a dedicated FOREIGN_ID only for the integer case — for everything else we
        # use the resolved physical type so the generated column type is faithful.
        ftype = _PHYSICAL_TO_FIELDTYPE.get(base, FieldType.FOREIGN_ID)
    else:
        ftype = _PHYSICAL_TO_FIELDTYPE.get(base, FieldType.VARCHAR)
    return SchemaField(
        name=field.name,
        type=ftype,
        nullable=field.nullable,
        primary_key=field.is_primary_key,
        auto_increment=field.auto_increment,
        default=field.default,
        length=physical.get("length"),
        precision=physical.get("precision"),
        scale=physical.get("scale"),
        enum_ref=enum_name,
        description=field.comment,
    )


def to_legacy_schema(data: dict[str, Any] | SchemaJson) -> DatabaseSchema:
    """Project a ``schema_json`` document onto a legacy :class:`DatabaseSchema` for code generation.

    Type resolution (incl. FK→PK physical inheritance), relations (id→name), reusable enums and
    physical indexes are all carried over. Presentation/semantic layers are display/business concerns
    that the code generators do not consume, so they are intentionally dropped.
    """
    schema = data if isinstance(data, SchemaJson) else core_sj.load(core_sj.migrate(data), validate=False)
    fk_physical = resolve_fk_physical(schema)
    fk_field_ids = {
        rel.foreign_key_field_id for rel in schema.logical.relations if rel.foreign_key_field_id
    }

    table_name_by_id = {t.id: t.name for t in schema.logical.tables}
    field_name_by_id = {f.id: f.name for t in schema.logical.tables for f in t.fields}
    enum_name_by_id = {e.id: e.name for e in schema.logical.enums}

    # Reusable enums (logical.enums) → legacy EnumDef (name + values).
    enums = [
        EnumDef(name=e.name, values=[v.value for v in e.values]) for e in schema.logical.enums
    ]

    # Indexes (physical.indexes) grouped per owning table; single-column unique indexes also flip the
    # column's own `unique`/`indexed` flags so the ORM generators render them.
    indexes_by_table: dict[str, list[Index]] = {}
    unique_cols: dict[str, set[str]] = {}
    indexed_cols: dict[str, set[str]] = {}
    if schema.physical:
        for idx in schema.physical.indexes:
            tname = table_name_by_id.get(idx.table_id)
            if not tname:
                continue
            cols = [field_name_by_id.get(c, c) for c in idx.columns]
            indexes_by_table.setdefault(tname, []).append(
                Index(name="", columns=cols, unique=idx.unique, type=idx.type or "btree")
            )
            if len(cols) == 1:
                (unique_cols if idx.unique else indexed_cols).setdefault(tname, set()).add(cols[0])

    tables: list[Table] = []
    for ct in schema.logical.tables:
        legacy_fields = [
            _field_to_legacy(f, fk_physical, fk_field_ids, enum_name_by_id.get(f.enum_id) if f.enum_id else None)
            for f in ct.fields
        ]
        for lf in legacy_fields:
            if lf.name in unique_cols.get(ct.name, set()):
                lf.unique = True
            if lf.name in indexed_cols.get(ct.name, set()):
                lf.indexed = True
        field_names = {f.name for f in ct.fields}
        tables.append(
            Table(
                name=ct.name,
                fields=legacy_fields,
                relations=_relations_for(ct, schema, table_name_by_id, field_name_by_id),
                indexes=indexes_by_table.get(ct.name, []),
                # Real created_at/updated_at/deleted_at columns drive the generators' timestamp/soft-delete
                # idioms (the field loop skips those names; the flag re-emits them once — no duplication).
                timestamps=bool(_TIMESTAMP_NAMES & field_names),
                soft_delete="deleted_at" in field_names,
                group=ct.domain,
                description=ct.comment,
            )
        )

    meta = schema.meta.model_dump(by_alias=True, exclude_none=True) if schema.meta else {}
    return DatabaseSchema(
        id=(meta.get("name") or "schema"),
        type="sql",
        driver="postgresql",
        tables=tables,
        enums=enums,
        metadata=meta,
    ).materialize_enums()


def _relations_for(
    table: CoreTable,
    schema: SchemaJson,
    table_name_by_id: dict[str, str],
    field_name_by_id: dict[str, str],
) -> list[Relation]:
    """Legacy name-based relations owned by ``table`` (those whose FK column lives on it)."""
    owns = {f.id for f in table.fields}
    out: list[Relation] = []
    for rel in schema.logical.relations:
        if rel.foreign_key_field_id and rel.foreign_key_field_id not in owns:
            continue
        if rel.from_table_id != table.id and rel.foreign_key_field_id is None:
            continue
        to_name = table_name_by_id.get(rel.to_table_id or "", "")
        if not to_name:
            continue
        out.append(
            Relation(
                name=rel.name or "",
                from_table=table.name,
                from_field=field_name_by_id.get(rel.foreign_key_field_id or "", ""),
                to_table=to_name,
                type=_REL_TYPE.get(rel.type, RelationType.MANY_TO_ONE),
                on_delete=(rel.on_delete or "cascade").lower(),
                on_update=(rel.on_update or "cascade").lower(),
            )
        )
    return out
