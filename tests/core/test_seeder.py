"""Unit tests for the scenario-based seeder (Milestone 3)."""

from __future__ import annotations

import json
import re

import pytest

from app.core import schema_json as sj
from app.core.seeder import DEFAULT_SEED, SeedError, seed_data, topological_order

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _ecommerce() -> dict:
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uemail", "name": "email", "semanticType": "email", "nullable": False}]},
                {"id": "tbl_orders001", "name": "orders", "kind": "normal", "fields": [
                    {"id": "fld_oid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ouser", "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_ostatus", "name": "status", "semanticType": "status", "nullable": False},
                    {"id": "fld_ototal", "name": "total", "semanticType": "money", "nullable": False}]},
                {"id": "tbl_pay00001", "name": "payments", "kind": "normal", "fields": [
                    {"id": "fld_pid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_porder", "name": "order_id", "semanticType": "foreign_key", "nullable": False},
                    {"id": "fld_pstatus", "name": "status", "semanticType": "status", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_ou", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_orders001", "toTableId": "tbl_users0001",
                 "foreignKeyFieldId": "fld_ouser", "onDelete": "cascade"},
                {"id": "rel_po", "name": "belongsTo", "type": "one_to_many",
                 "fromTableId": "tbl_pay00001", "toTableId": "tbl_orders001",
                 "foreignKeyFieldId": "fld_porder", "onDelete": "cascade"},
            ],
        },
        "semantic": {"stateMachines": [{
            "id": "sm_order", "name": "OrderStatus", "fieldId": "fld_ostatus",
            "states": [{"id": "stt_p", "name": "pending", "initial": True},
                       {"id": "stt_d", "name": "delivered"},
                       {"id": "stt_c", "name": "cancelled", "final": True}],
            "transitions": [{"id": "trn_pd", "name": "deliver", "from": "stt_p", "to": "stt_d"},
                            {"id": "trn_pc", "name": "cancel", "from": "stt_p", "to": "stt_c"}],
        }]},
    }


_SCENARIO = {"name": "ecommerce_medium", "params": {"users": 3, "orders": 10}}


def test_rows_and_topological_order():
    out = seed_data(_ecommerce(), seed=42, scenario=_SCENARIO, output="json")
    assert out["rows"] == {"users": 3, "orders": 10, "payments": 6}
    order = [t.name for t in topological_order(sj.load(_ecommerce(), validate=False))]
    assert order.index("users") < order.index("orders") < order.index("payments")


def test_fk_values_reference_real_rows_and_are_uuids():
    out = seed_data(_ecommerce(), seed=42, scenario=_SCENARIO, output="json")
    users, orders, pays = out["data"]["users"], out["data"]["orders"], out["data"]["payments"]
    user_ids = {u["id"] for u in users}
    order_ids = {o["id"] for o in orders}
    assert all(_UUID_RE.match(o["user_id"]) for o in orders)          # uuid, not an int
    assert all(o["user_id"] in user_ids for o in orders)               # references a real user
    assert all(p["order_id"] in order_ids for p in pays)               # references a real order


def test_status_distribution_respects_reachable_states():
    out = seed_data(_ecommerce(), seed=42, scenario=_SCENARIO, output="json")
    statuses = [o["status"] for o in out["data"]["orders"]]
    assert statuses.count("delivered") == 6
    assert statuses.count("pending") == 2
    assert statuses.count("cancelled") == 2
    assert set(statuses) <= {"pending", "delivered", "cancelled"}  # only reachable states


def test_derive_creates_payment_only_for_delivered_orders():
    out = seed_data(_ecommerce(), seed=42, scenario=_SCENARIO, output="json")
    orders, pays = out["data"]["orders"], out["data"]["payments"]
    delivered = {o["id"] for o in orders if o["status"] == "delivered"}
    assert len(pays) == len(delivered)
    assert all(p["order_id"] in delivered for p in pays)
    assert all(p["status"] == "success" for p in pays)  # the scenario's `set`


def test_unique_email_has_no_duplicates():
    schema = _ecommerce()
    schema["physical"] = {"indexes": [
        {"id": "idx_uemail", "tableId": "tbl_users0001", "columns": ["fld_uemail"], "unique": True}]}
    out = seed_data(schema, seed=1, scenario={"params": {"users": 25}}, output="json")
    emails = [u["email"] for u in out["data"]["users"]]
    assert len(emails) == len(set(emails))


def test_determinism_same_seed_byte_identical():
    a = seed_data(_ecommerce(), seed=99, scenario=_SCENARIO, output="json")["data"]
    b = seed_data(_ecommerce(), seed=99, scenario=_SCENARIO, output="json")["data"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_different_seed_differs():
    a = seed_data(_ecommerce(), seed=1, scenario=_SCENARIO, output="json")["data"]
    b = seed_data(_ecommerce(), seed=2, scenario=_SCENARIO, output="json")["data"]
    assert a["users"][0]["id"] != b["users"][0]["id"]


def test_default_seed_is_fixed_not_random():
    a = seed_data(_ecommerce(), scenario=_SCENARIO, output="json")["data"]
    b = seed_data(_ecommerce(), seed=DEFAULT_SEED, scenario=_SCENARIO, output="json")["data"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_no_llm_produces_complete_valid_data():
    # The whole seeder runs with no LLM at all; every non-nullable column is populated.
    out = seed_data(_ecommerce(), seed=5, scenario=_SCENARIO, output="json")
    for row in out["data"]["orders"]:
        assert row["user_id"] and row["status"] and row["total"] is not None


def test_sql_output_is_topological_and_quoted():
    out = seed_data(_ecommerce(), seed=42, scenario=_SCENARIO, output="sql")
    stmts = out["sql"]["statements"]
    assert out["sql"]["driver"] == "postgres"
    first_order = next(i for i, s in enumerate(stmts) if "INSERT INTO \"orders\"" in s)
    first_user = next(i for i, s in enumerate(stmts) if "INSERT INTO \"users\"" in s)
    first_payment = next(i for i, s in enumerate(stmts) if "INSERT INTO \"payments\"" in s)
    assert first_user < first_order < first_payment  # FK targets inserted first


def test_not_null_fk_cycle_raises():
    schema = {
        "formatVersion": "1.0.0",
        "logical": {
            "tables": [
                {"id": "tbl_a", "name": "a", "kind": "normal", "fields": [
                    {"id": "fld_aid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ab", "name": "b_id", "semanticType": "foreign_key", "nullable": False}]},
                {"id": "tbl_b", "name": "b", "kind": "normal", "fields": [
                    {"id": "fld_bid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ba", "name": "a_id", "semanticType": "foreign_key", "nullable": False}]},
            ],
            "relations": [
                {"id": "rel_ab", "type": "one_to_many", "fromTableId": "tbl_a", "toTableId": "tbl_b",
                 "foreignKeyFieldId": "fld_ab"},
                {"id": "rel_ba", "type": "one_to_many", "fromTableId": "tbl_b", "toTableId": "tbl_a",
                 "foreignKeyFieldId": "fld_ba"},
            ],
        },
    }
    with pytest.raises(SeedError):
        seed_data(schema, seed=1, output="json")


def test_nullable_fk_cycle_is_broken_not_raised():
    schema = {
        "formatVersion": "1.0.0",
        "logical": {
            "tables": [
                {"id": "tbl_a", "name": "a", "kind": "normal", "fields": [
                    {"id": "fld_aid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ab", "name": "b_id", "semanticType": "foreign_key", "nullable": True}]},
                {"id": "tbl_b", "name": "b", "kind": "normal", "fields": [
                    {"id": "fld_bid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_ba", "name": "a_id", "semanticType": "foreign_key", "nullable": True}]},
            ],
            "relations": [
                {"id": "rel_ab", "type": "one_to_many", "fromTableId": "tbl_a", "toTableId": "tbl_b",
                 "foreignKeyFieldId": "fld_ab"},
                {"id": "rel_ba", "type": "one_to_many", "fromTableId": "tbl_b", "toTableId": "tbl_a",
                 "foreignKeyFieldId": "fld_ba"},
            ],
        },
    }
    out = seed_data(schema, seed=1, scenario={"params": {"a": 2, "b": 2}}, output="json")
    assert out["rows"] == {"a": 2, "b": 2}


def test_integer_primary_key_is_sequential():
    schema = {
        "formatVersion": "1.0.0",
        "logical": {"tables": [{"id": "tbl_l", "name": "logs", "kind": "normal", "fields": [
            {"id": "fld_lid", "name": "id", "semanticType": "big_integer",
             "isPrimaryKey": True, "autoIncrement": True, "nullable": False},
            {"id": "fld_lmsg", "name": "msg", "semanticType": "string", "nullable": False}]}]},
    }
    out = seed_data(schema, seed=1, scenario={"params": {"logs": 3}}, output="json")
    assert [r["id"] for r in out["data"]["logs"]] == [1, 2, 3]
