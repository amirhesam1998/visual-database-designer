"""Export-formats milestone §1/§4 — deterministic text exports from ``schema_json``.

YAML, DBML, JSON Schema and a Markdown data dictionary are generated in the engine (golden rule),
byte-for-byte deterministic (no LLM), from the layered schema with **resolved** types. The acceptance
criterion that matters: a foreign-key column shows the referenced primary key's physical type — a
uuid FK is ``uuid`` in every format, never an integer (the lesson of the whole project).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core import schema_export as se
from app.core import schema_json as sj
from app.module import app

client = TestClient(app)


def _schema() -> dict:
    """users (uuid PK, email=PII, status=enum) ← orders.user_id (FK onto the uuid PK)."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "comment": "Application users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail001", "name": "email", "semanticType": "email", "nullable": False},
                    {"id": "fld_ustatus01", "name": "status", "semanticType": "enum",
                     "enumId": "enm_status001", "nullable": False},
                ]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser0001", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_ototal001", "name": "total", "semanticType": "money", "nullable": False},
                ]},
            ],
            "relations": [
                {"id": "rel_order_usr", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser0001", "onDelete": "cascade"},
            ],
            "enums": [{"id": "enm_status001", "name": "user_status",
                       "values": [{"value": "active"}, {"value": "banned"}]}],
        },
        "physical": {"indexes": [{"id": "idx_users_em", "tableId": "tbl_users0001",
                                  "columns": ["fld_uemail001"], "unique": True}]},
    }


def _loaded():
    return sj.load(_schema(), validate=False)


# --- determinism (spec §4: same schema → same output) ---------------------------------------------


def test_all_text_exports_are_deterministic():
    schema = _loaded()
    for kind in se.SUPPORTED_KINDS:
        first, _ = se.export(schema, kind)
        second, _ = se.export(sj.load(_schema(), validate=False), kind)
        assert first == second, f"{kind} is not deterministic"
        assert first.strip(), f"{kind} produced empty output"


# --- the uuid-FK acceptance, per format -----------------------------------------------------------


def test_yaml_keeps_fk_as_uuid_and_lists_enums():
    content, lang = se.export(_loaded(), "yaml")
    assert lang == "yaml"
    import yaml
    doc = yaml.safe_load(content)
    orders = next(t for t in doc["tables"] if t["name"] == "orders")
    user_id = next(c for c in orders["columns"] if c["name"] == "user_id")
    assert user_id["type"] == "uuid"  # not integer/bigint
    assert user_id["references"] == "users.id"
    assert doc["enums"][0]["name"] == "user_status"
    # the resolved-type rule: the email column is varchar, not a number
    users = next(t for t in doc["tables"] if t["name"] == "users")
    assert next(c for c in users["columns"] if c["name"] == "email")["type"].startswith("varchar")


def test_dbml_renders_inline_fk_ref_with_uuid():
    content, _ = se.export(_loaded(), "dbml")
    assert "Table users {" in content and "Table orders {" in content
    assert "user_id uuid [not null, ref: > users.id]" in content
    assert "Enum user_status {" in content
    assert "indexes {" in content and "email [unique]" in content  # the index block
    assert "sensitive: none" not in content  # no spurious notes on non-PII columns


def test_json_schema_maps_uuid_and_required():
    content, lang = se.export(_loaded(), "jsonschema")
    assert lang == "json"
    doc = json.loads(content)
    orders = doc["$defs"]["orders"]
    assert orders["properties"]["user_id"] == {"type": "string", "format": "uuid"}
    assert orders["properties"]["total"]["type"] == "number"
    assert set(orders["required"]) == {"id", "user_id", "total"}
    assert doc["$defs"]["users"]["properties"]["status"]["enum"] == ["active", "banned"]


def test_data_dictionary_marks_pii_and_relations():
    content, lang = se.export(_loaded(), "datadict")
    assert lang == "markdown"
    assert content.startswith("# Data Dictionary — shop")
    assert "## users" in content and "Application users" in content
    assert "| email |" in content and "PII" in content  # email flagged sensitive
    assert "FK → users.id" in content  # relation surfaced
    assert "`uuid`" in content  # resolved type, not a number
    assert "user_status" in content  # enum section


def test_unknown_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        se.export(_loaded(), "cobol")


# --- HTTP wiring (Code panel calls /design/code with these kinds) ---------------------------------


def test_design_code_serves_each_text_format():
    schema = _schema()
    for kind, needle in [("yaml", "user_id"), ("dbml", "ref: > users.id"),
                         ("jsonschema", "$defs"), ("datadict", "Data Dictionary")]:
        res = client.post("/design/code", json={"schema_json": schema, "kind": kind})
        assert res.status_code == 200, (kind, res.text)
        body = res.json()
        assert body["kind"] == kind
        assert needle in body["content"]


def test_code_frameworks_advertises_text_formats():
    body = client.get("/design/code/frameworks").json()
    assert set(body["text"]) == set(se.SUPPORTED_KINDS)
