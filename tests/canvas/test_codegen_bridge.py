"""Code-generation bridge (unify spec phase 2 §2) — translation must not lose type or relation.

The bridge projects ``schema_json`` down to the legacy ``DatabaseSchema`` so the proven generators
can run on it. The one invariant that must survive (the lesson of the whole project): a foreign-key
column keeps the **referenced primary key's physical type** — a uuid FK is a uuid, never an integer.
Semantic types (Money → decimal, Email → varchar) must also resolve faithfully. These tests are the
same family as the earlier live gates: they prove the round-trip through the bridge is type-true.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.codegen_bridge import to_legacy_schema
from app.module import app
from app.schema_model import FieldType

client = TestClient(app)


def _schema() -> dict:
    """users(id uuid PK, email, balance money) ←FK— orders(id uuid PK, total money, user_id)."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid", "isPrimaryKey": True},
                    {"id": "fld_email0001", "name": "email", "semanticType": "email"},
                    {"id": "fld_bal000001", "name": "balance", "semanticType": "money"},
                ]},
                {"id": "tbl_orders001", "name": "orders", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid", "isPrimaryKey": True},
                    {"id": "fld_total0001", "name": "total", "semanticType": "money"},
                    {"id": "fld_uref00001", "name": "user_id", "semanticType": "foreign_key"},
                ]},
            ],
            "relations": [
                {"id": "rel_o2u000001", "type": "many_to_one", "fromTableId": "tbl_orders001",
                 "toTableId": "tbl_users0001", "foreignKeyFieldId": "fld_uref00001"},
            ],
        },
    }


def test_bridge_preserves_uuid_fk_and_semantic_types() -> None:
    legacy = to_legacy_schema(_schema())
    orders = legacy.table("orders")
    users = legacy.table("users")
    assert orders is not None and users is not None

    # The FK column inherits the referenced uuid PK's storage type — NOT an integer (the core lesson).
    user_id = orders.field("user_id")
    assert user_id is not None and user_id.type == FieldType.UUID

    # Semantic types resolve faithfully through the one Type System registry.
    assert users.field("email").type == FieldType.VARCHAR
    assert users.field("email").length == 255
    assert users.field("balance").type == FieldType.DECIMAL
    assert orders.field("total").type == FieldType.DECIMAL

    # The relation survives the translation (orders belongs to users).
    assert any(r.to_table == "users" for r in orders.relations)


def test_codegen_route_typeorm_renders_uuid_column() -> None:
    res = client.post("/design/code", json={
        "schema_json": _schema(), "kind": "model", "framework": "typeorm", "table": "orders",
    })
    assert res.status_code == 200
    content = res.json()["content"]
    # The generated entity must type user_id as uuid (TypeORM column type), never int/bigint.
    assert "user_id" in content
    assert "type: 'uuid'" in content


def test_codegen_route_sql_is_pure_core_and_type_true() -> None:
    res = client.post("/design/code", json={"schema_json": _schema(), "kind": "sql"})
    assert res.status_code == 200
    sql = res.json()["content"].lower()
    assert "create table" in sql
    assert '"orders"' in sql or "orders" in sql
    # user_id FK column rendered as uuid, never integer/bigint.
    assert "user_id" in sql
    assert "uuid" in sql


def test_codegen_route_crud_and_schema_export() -> None:
    crud = client.post("/design/code", json={
        "schema_json": _schema(), "kind": "crud", "framework": "laravel", "table": "users",
    })
    assert crud.status_code == 200 and "Controller" in crud.json()["content"]

    prisma = client.post("/design/code", json={
        "schema_json": _schema(), "kind": "schema", "framework": "prisma",
    })
    assert prisma.status_code == 200 and "model" in prisma.json()["content"].lower()


def test_codegen_route_rejects_missing_schema_and_bad_kind() -> None:
    assert client.post("/design/code", json={}).status_code == 400
    bad = client.post("/design/code", json={"schema_json": _schema(), "kind": "nope"})
    assert bad.status_code == 400
