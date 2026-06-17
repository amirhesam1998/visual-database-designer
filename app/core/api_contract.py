"""API Contract — OpenAPI 3.1 generated deterministically from ``schema_json`` (Milestone 4).

This is the milestone's headline artifact: a single **source of truth** for everything downstream
(the reference server, the client, validation). Routes/validation/clients are derived from the
OpenAPI document (and the compact :func:`build_contract` it shares), never re-derived from the schema,
so all generators agree.

Mapping rules (spec §2/§4):

* each table → a REST resource with CRUD paths under ``/{version}``;
* each table → an output schema (``Order``) and an input schema (``OrderCreate``) — read-only fields
  (primary key, auto-increment, auto timestamps) appear only in the output;
* each field's type comes from the *resolved* Type System type (its ``openapi`` projection), never the
  raw physical type — and a **foreign-key field inherits the referenced primary key's type** via
  :func:`resolve_fk_physical` (so ``orders.user_id`` is a ``string/uuid``, never an integer — the
  lesson M1/M2/M3 keep proving);
* ``required`` follows ``nullable:false`` + no default; enum/status columns expose their reachable
  state-machine states as an ``enum``;
* errors are documented with an RFC 7807 ``Problem`` schema.

Everything here is pure and deterministic (sorted keys, no set iteration) so the same schema yields a
byte-identical document across runs and processes. The LLM (if any) only enriches descriptions/examples.
"""

from __future__ import annotations

from typing import Any

from app.core import schema_json as core_sj
from app.core import state_machine as core_sm
from app.core.schema_json import Field_, SchemaJson, Table
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, UnsupportedPhysicalTypeError

# Columns that are server-managed and therefore read-only (excluded from the create/update input).
_AUTO_TIMESTAMPS = {"created_at", "updated_at", "deleted_at"}


def _pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in (name or "").split("_") if w)


def _singular(name: str) -> str:
    n = name or ""
    low = n.lower()
    for suffix, repl in (("ies", "y"), ("ses", "s"), ("xes", "x"), ("ches", "ch"), ("shes", "sh")):
        if low.endswith(suffix):
            return n[: -len(suffix)] + repl
    if low.endswith("s") and not low.endswith("ss"):
        return n[:-1]
    return n


def _model_name(table: Table) -> str:
    return _pascal(_singular(table.name)) or _pascal(table.name) or "Resource"


def _is_read_only(field: Field_) -> bool:
    return field.is_primary_key or field.auto_increment or field.name in _AUTO_TIMESTAMPS


# --------------------------------------------------------------------------------------------------
# Field → OpenAPI schema (resolved Type System type; FK inherits the referenced PK's type).
# --------------------------------------------------------------------------------------------------
def _field_openapi(field: Field_, schema: SchemaJson, reg: TypeRegistry,
                   fk_targets: dict[str, Field_], sm_by_field: dict[str, Any]) -> dict[str, Any]:
    # 1. Foreign key → the referenced primary key's OpenAPI type (uuid stays uuid, not integer).
    target_pk = fk_targets.get(field.id)
    if target_pk is not None:
        try:
            spec = dict(reg.resolve(target_pk, "postgres").openapi)
        except (KeyError, UnsupportedPhysicalTypeError):
            spec = {"type": "string"}
        spec["description"] = f"Foreign key -> {field.name.removesuffix('_id') or field.name}"
        return spec

    # 2. Enum / status column → expose the (reachable) allowed values as an enum.
    allowed = _allowed_values(field, schema, sm_by_field)
    if allowed is not None:
        return {"type": "string", "enum": allowed}

    # 3. Otherwise the resolved semantic type's OpenAPI projection.
    try:
        spec = dict(reg.resolve(field, "postgres").openapi)
    except (KeyError, UnsupportedPhysicalTypeError):
        spec = {"type": "string"}
    if field.overrides and field.overrides.physical and "length" in field.overrides.physical:
        spec.setdefault("maxLength", field.overrides.physical["length"])
    elif spec.get("format") is None and field.semantic_type in {"string", "slug"}:
        spec.setdefault("maxLength", 255)
    return spec


def _allowed_values(field: Field_, schema: SchemaJson, sm_by_field: dict[str, Any]) -> list[str] | None:
    sm = sm_by_field.get(field.id)
    if sm is not None:
        reachable = list(core_sm.seeder_plan(sm).keys())
        if reachable:
            return reachable
    if field.enum_id:
        enum = schema.enum_by_id(field.enum_id)
        if enum:
            return [v.value for v in enum.values]
    return None


# --------------------------------------------------------------------------------------------------
# The compact contract — the single source of truth shared by OpenAPI + server + client.
# --------------------------------------------------------------------------------------------------
def build_contract(schema_json: dict[str, Any], *, version: str = "v1",
                   registry: TypeRegistry | None = None) -> dict[str, Any]:
    """Derive a compact, JSON-able contract from ``schema_json`` (resources, fields, FKs, state machines)."""
    reg = registry or DEFAULT_REGISTRY
    schema = core_sj.load(core_sj.migrate(schema_json), validate=False)

    fk_targets: dict[str, Field_] = {}     # fk field id → referenced PK field
    fk_meta: dict[str, dict[str, str]] = {}  # fk field id → {table, column}
    for rel in schema.logical.relations:
        if rel.foreign_key_field_id and rel.to_table_id:
            to_table = schema.table_by_id(rel.to_table_id)
            if to_table and to_table.primary_keys():
                pk = to_table.primary_keys()[0]
                fk_targets[rel.foreign_key_field_id] = pk
                fk_meta[rel.foreign_key_field_id] = {"table": to_table.name, "column": pk.name}

    sm_by_field = {sm.field_id: sm for sm in (schema.semantic.state_machines if schema.semantic else [])}

    # parent table id → [{child table, fk column, path}] for nested one_to_many reads.
    nested: dict[str, list[dict[str, str]]] = {}
    for rel in schema.logical.relations:
        if rel.type in {"one_to_many"} and rel.from_table_id and rel.to_table_id and rel.foreign_key_field_id:
            child = schema.table_by_id(rel.from_table_id)
            fk_field = child.field_by_id(rel.foreign_key_field_id) if child else None
            if child and fk_field:
                nested.setdefault(rel.to_table_id, []).append(
                    {"child": child.name, "fk": fk_field.name, "path": child.name})

    resources: list[dict[str, Any]] = []
    for table in sorted(schema.logical.tables, key=lambda t: t.name):
        pks = table.primary_keys()
        pk = pks[0] if pks else None
        fields_out: list[dict[str, Any]] = []
        for field in table.fields:
            entry: dict[str, Any] = {
                "name": field.name,
                "openapi": _field_openapi(field, schema, reg, fk_targets, sm_by_field),
                "readOnly": _is_read_only(field),
                "required": (not field.nullable) and field.default is None and not _is_read_only(field),
                "pk": field.is_primary_key,
            }
            if field.id in fk_meta:
                entry["fk"] = fk_meta[field.id]
            sm = sm_by_field.get(field.id)
            if sm is not None:
                plan = core_sm.seeder_plan(sm)
                initial = next((s.name for s in sm.states if s.initial), None)
                transitions = sorted(
                    [core_sm._state_name(sm, t.from_), core_sm._state_name(sm, t.to)]
                    for t in sm.transitions
                    if core_sm._state_name(sm, t.from_) in plan and core_sm._state_name(sm, t.to) in plan
                )
                entry["stateMachine"] = {"initial": initial, "transitions": transitions}
            fields_out.append(entry)

        resources.append({
            "table": table.name,
            "model": _model_name(table),
            "pk": pk.name if pk else None,
            "pkType": (pk.semantic_type if pk else None),
            "pkIsUuid": bool(pk and reg.has(pk.semantic_type)
                             and reg.get(pk.semantic_type).physical_for("postgres").type == "uuid"),
            "pkAutoIncrement": bool(pk and pk.auto_increment),
            "fields": fields_out,
            "nested": sorted(nested.get(table.id, []), key=lambda n: n["path"]),
        })
    return {"version": version, "resources": resources}


# --------------------------------------------------------------------------------------------------
# Contract → OpenAPI 3.1 document.
# --------------------------------------------------------------------------------------------------
def _problem_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": "RFC 7807 problem detail.",
        "properties": {
            "type": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "integer"},
            "detail": {"type": "string"},
            "errors": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "field": {"type": "string"}, "message": {"type": "string"}}},
            },
        },
        "required": ["title", "status"],
    }


def _problem_response(description: str) -> dict[str, Any]:
    return {"description": description,
            "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}


def build_openapi(schema_json: dict[str, Any], *, version: str = "v1",
                  registry: TypeRegistry | None = None, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render a deterministic OpenAPI 3.1 document for ``schema_json`` (or a pre-built ``contract``)."""
    contract = contract or build_contract(schema_json, version=version, registry=registry)
    prefix = f"/{version}"

    schemas: dict[str, Any] = {"Problem": _problem_schema()}
    paths: dict[str, Any] = {}

    for res in contract["resources"]:
        model = res["model"]
        table = res["table"]
        # Output schema: every field (incl. read-only). Input schema: writable fields only.
        out_props: dict[str, Any] = {}
        in_props: dict[str, Any] = {}
        required_in: list[str] = []
        for f in res["fields"]:
            prop = dict(f["openapi"])
            out_props[f["name"]] = ({**prop, "readOnly": True} if f["readOnly"] else prop)
            if not f["readOnly"]:
                in_props[f["name"]] = dict(f["openapi"])
                if f["required"]:
                    required_in.append(f["name"])
        schemas[model] = {"type": "object", "properties": out_props}
        create: dict[str, Any] = {"type": "object", "properties": in_props}
        if required_in:
            create["required"] = required_in
        schemas[f"{model}Create"] = create
        # Update: every writable field, all optional (PATCH semantics).
        schemas[f"{model}Update"] = {"type": "object", "properties": {k: dict(v) for k, v in in_props.items()}}

        ref_out = {"$ref": f"#/components/schemas/{model}"}
        ref_create = {"$ref": f"#/components/schemas/{model}Create"}
        ref_update = {"$ref": f"#/components/schemas/{model}Update"}

        paths[f"{prefix}/{table}"] = {
            "get": {
                "summary": f"List {table}", "operationId": f"list_{table}",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50, "maximum": 200}},
                    {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0, "minimum": 0}},
                ],
                "responses": {"200": {"description": "OK", "content": {"application/json": {
                    "schema": {"type": "array", "items": ref_out}}}}},
            },
            "post": {
                "summary": f"Create {model}", "operationId": f"create_{table}",
                "requestBody": {"required": True, "content": {"application/json": {"schema": ref_create}}},
                "responses": {
                    "201": {"description": "Created", "content": {"application/json": {"schema": ref_out}}},
                    "409": _problem_response("Conflict (unique or foreign-key violation)"),
                    "422": _problem_response("Validation error"),
                },
            },
        }
        item: dict[str, Any] = {
            "get": {
                "summary": f"Get {model}", "operationId": f"get_{table}",
                "parameters": [_id_param()],
                "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": ref_out}}},
                              "404": _problem_response("Not found")},
            },
            "patch": {
                "summary": f"Update {model}", "operationId": f"update_{table}",
                "parameters": [_id_param()],
                "requestBody": {"required": True, "content": {"application/json": {"schema": ref_update}}},
                "responses": {
                    "200": {"description": "Updated", "content": {"application/json": {"schema": ref_out}}},
                    "404": _problem_response("Not found"),
                    "409": _problem_response("Conflict"),
                    "422": _problem_response("Validation error (incl. illegal state transition)"),
                },
            },
            "delete": {
                "summary": f"Delete {model}", "operationId": f"delete_{table}",
                "parameters": [_id_param()],
                "responses": {"204": {"description": "Deleted"}, "404": _problem_response("Not found")},
            },
        }
        paths[f"{prefix}/{table}/{{id}}"] = item

        for nest in res["nested"]:
            child_model = next(
                (r["model"] for r in contract["resources"] if r["table"] == nest["child"]),
                nest["child"],
            )
            paths[f"{prefix}/{table}/{{id}}/{nest['path']}"] = {
                "get": {
                    "summary": f"List {nest['child']} for a {model}",
                    "operationId": f"list_{table}_{nest['path']}",
                    "parameters": [_id_param()],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {
                        "schema": {"type": "array", "items": {"$ref": f"#/components/schemas/{child_model}"}}}}},
                        "404": _problem_response("Parent not found")},
                },
            }

    return {
        "openapi": "3.1.0",
        "info": {"title": "Generated API", "version": "1.0.0",
                 "description": "Deterministically generated from schema_json by the Visual Database Designer."},
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {"schemas": schemas},
    }


def _id_param() -> dict[str, Any]:
    return {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


def contract_stats(openapi: dict[str, Any]) -> dict[str, int]:
    paths = openapi.get("paths", {})
    operations = sum(len([m for m in methods if m in {"get", "post", "patch", "put", "delete"}])
                     for methods in paths.values())
    resources = sum(1 for name in openapi.get("components", {}).get("schemas", {})
                    if name != "Problem" and not name.endswith("Create") and not name.endswith("Update"))
    return {"resources": resources, "paths": len(paths), "operations": operations}
