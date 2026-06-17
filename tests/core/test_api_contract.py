"""Unit tests for the Milestone 4 API generators (offline, no database).

Covers the three deterministic generators in isolation:

* :mod:`app.core.api_contract` — the compact contract + OpenAPI 3.1 document (shape, FK-type, enum,
  read-only, required, RFC 7807, determinism);
* :mod:`app.core.api_server` — the standalone FastAPI server (compiles, builds an app, validates a
  request *before* the database so a bad body → 422 problem+json with no DB connection);
* :mod:`app.core.api_client` — the TypeScript client + Postman collection (shape, determinism).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.api_client import generate_client
from app.core.api_contract import build_contract, build_openapi, contract_stats
from app.core.api_server import generate_server_files


def _ecommerce_schema() -> dict:
    """The M1/M2/M3 reference schema: uuid PK, a uuid FK, an enum-backed status state machine."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False}]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_ostatus01", "name": "status", "semanticType": "status", "nullable": False},
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
        },
        "semantic": {"stateMachines": [{
            "id": "sm_order_status", "name": "OrderStatus", "fieldId": "fld_ostatus01",
            "states": [{"id": "stt_pending", "name": "pending", "initial": True},
                       {"id": "stt_delivered", "name": "delivered"},
                       {"id": "stt_cancelled", "name": "cancelled", "final": True}],
            "transitions": [{"id": "trn_deliver", "name": "deliver", "from": "stt_pending", "to": "stt_delivered"},
                            {"id": "trn_cancel", "name": "cancel", "from": "stt_pending", "to": "stt_cancelled"}],
        }]},
    }


# --- contract / OpenAPI -------------------------------------------------------------------------


def test_contract_shape_and_fk_type():
    contract = build_contract(_ecommerce_schema())
    by_table = {r["table"]: r for r in contract["resources"]}
    assert set(by_table) == {"users", "orders"}

    orders = by_table["orders"]
    assert orders["model"] == "Order"
    assert orders["pk"] == "id" and orders["pkType"] == "uuid" and orders["pkIsUuid"] is True

    fields = {f["name"]: f for f in orders["fields"]}
    # FK field inherits the referenced PK's type — uuid, not integer (the M1/M2/M3 lesson).
    assert fields["user_id"]["openapi"] == {"type": "string", "format": "uuid",
                                            "description": "Foreign key -> user"}
    assert fields["user_id"]["fk"] == {"table": "users", "column": "id"}
    # status exposes its reachable state-machine states as an enum.
    assert fields["status"]["openapi"] == {"type": "string", "enum": ["pending", "delivered", "cancelled"]}
    assert fields["status"]["stateMachine"]["initial"] == "pending"
    assert fields["status"]["stateMachine"]["transitions"] == [["pending", "cancelled"], ["pending", "delivered"]]


def test_openapi_read_only_and_required():
    openapi = build_openapi(_ecommerce_schema())
    schemas = openapi["components"]["schemas"]
    # The read-only PK appears in the output model but not in the create input.
    assert "id" in schemas["Order"]["properties"]
    assert schemas["Order"]["properties"]["id"]["readOnly"] is True
    assert "id" not in schemas["OrderCreate"]["properties"]
    # required follows nullable:false + no default + writable.
    assert sorted(schemas["OrderCreate"]["required"]) == ["status", "total", "user_id"]
    # Update is all-optional (PATCH semantics).
    assert "required" not in schemas["OrderUpdate"]


def test_openapi_paths_and_problem_model():
    openapi = build_openapi(_ecommerce_schema())
    assert openapi["openapi"] == "3.1.0"
    paths = openapi["paths"]
    assert "/v1/orders" in paths and "/v1/orders/{id}" in paths
    # nested one_to_many read.
    assert "/v1/users/{id}/orders" in paths
    post = paths["/v1/orders"]["post"]
    assert set(post["responses"]) == {"201", "409", "422"}
    # RFC 7807 documented and referenced.
    assert "Problem" in openapi["components"]["schemas"]
    ref = post["responses"]["422"]["content"]["application/problem+json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/Problem"


def test_contract_stats():
    openapi = build_openapi(_ecommerce_schema())
    stats = contract_stats(openapi)
    assert stats["resources"] == 2
    assert stats["paths"] == len(openapi["paths"])
    assert stats["operations"] > 0


def test_openapi_is_deterministic():
    a = json.dumps(build_openapi(_ecommerce_schema()), sort_keys=True)
    b = json.dumps(build_openapi(_ecommerce_schema()), sort_keys=True)
    assert a == b


def test_custom_version_prefix():
    openapi = build_openapi(_ecommerce_schema(), version="v2")
    assert "/v2/orders" in openapi["paths"]


# --- generated server (compiles, validates before DB) ------------------------------------------


def _build_app(schema_dict: dict):
    files = generate_server_files(schema_dict, version="v1")
    namespace: dict = {"__name__": "generated_main"}
    exec(compile(files["main.py"], "generated_main.py", "exec"), namespace)  # noqa: S102 - generated artifact
    return namespace["app"]


def test_server_files_compile_and_are_deterministic():
    a = generate_server_files(_ecommerce_schema())
    b = generate_server_files(_ecommerce_schema())
    assert a == b                                   # byte-identical across runs
    assert set(a) == {"main.py", "requirements.txt", "README.md"}
    compile(a["main.py"], "main.py", "exec")        # valid Python


def test_server_health_endpoint():
    client = TestClient(_build_app(_ecommerce_schema()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert set(resp.json()["resources"]) == {"users", "orders"}


def test_server_rejects_numeric_fk_before_db():
    """The FK-type lesson at the HTTP layer: a numeric user_id fails string/uuid validation → 422,
    and crucially this happens *before* any database connection (DATABASE_URL is never read)."""
    client = TestClient(_build_app(_ecommerce_schema()))
    resp = client.post("/v1/orders", json={"user_id": 123, "status": "pending", "total": 10})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 422
    assert any(e["field"] == "user_id" for e in body["errors"])


def test_server_rejects_missing_required_and_bad_email_before_db():
    client = TestClient(_build_app(_ecommerce_schema()))
    # missing required email.
    resp = client.post("/v1/users", json={})
    assert resp.status_code == 422
    assert any(e["field"] == "email" and e["message"] == "required" for e in resp.json()["errors"])
    # bad email format.
    resp = client.post("/v1/users", json={"email": "not-an-email"})
    assert resp.status_code == 422
    assert any(e["field"] == "email" for e in resp.json()["errors"])


def test_server_rejects_read_only_field():
    client = TestClient(_build_app(_ecommerce_schema()))
    resp = client.post("/v1/orders",
                       json={"id": "x", "user_id": "00000000-0000-4000-8000-000000000000",
                             "status": "pending", "total": 10})
    assert resp.status_code == 422
    assert any(e["field"] == "id" for e in resp.json()["errors"])


# --- client / Postman --------------------------------------------------------------------------


def test_client_generation_shape_and_determinism():
    openapi = build_openapi(_ecommerce_schema())
    a = generate_client(openapi, target="typescript")
    b = generate_client(openapi, target="typescript")
    assert a == b
    assert "client.ts" in a["files"]
    assert "export class ApiClient" in a["files"]["client.ts"]
    assert "async listOrders" in a["files"]["client.ts"]
    assert a["postman"]["info"]["name"] == "Generated API"
    assert a["postman"]["item"]


def test_client_unsupported_target_raises():
    openapi = build_openapi(_ecommerce_schema())
    with pytest.raises(ValueError):
        generate_client(openapi, target="cobol")
