"""Shared, deterministic fixtures for the Core conformance kits.

Every id here is a fixed literal (not minted) so snapshots are stable across runs. The canonical
schema is a small but representative e-commerce model: ``users`` (with a Status field driven by an
``order`` would be odd, so the Status/state-machine lives on ``orders``), ``orders`` referencing
``users``, a reusable enum, an index, ownership/tenancy, a business rule and one state machine —
i.e. it exercises all five layers.
"""

from __future__ import annotations

from typing import Any

# Stable literal ids (valid against the JSON Schema id pattern).
TBL_USERS = "tbl_users0001"
TBL_ORDERS = "tbl_orders001"
FLD_U_ID = "fld_uid000001"
FLD_U_EMAIL = "fld_uemail001"
FLD_U_NAME = "fld_uname0001"
FLD_O_ID = "fld_oid000001"
FLD_O_USER = "fld_ouser0001"
FLD_O_TOTAL = "fld_ototal001"
FLD_O_STATUS = "fld_ostatus01"
REL_ORDER_USER = "rel_order_usr"
IDX_U_EMAIL = "idx_uemail001"
ENM_PRIORITY = "enm_priority1"
SM_ORDER = "sm_order00001"
STT_PENDING = "stt_pending01"
STT_PAID = "stt_paid00001"
STT_SHIPPED = "stt_shipped01"
STT_CANCELLED = "stt_cancel001"
TRN_PAY = "trn_pay000001"
TRN_SHIP = "trn_ship00001"
TRN_CANCEL = "trn_cancel001"
# The id pattern only allows the entity prefixes tbl|fld|rel|idx|enm|sm|stt|trn, so a business-rule
# id has to reuse one of them; we use the table prefix as a stable, schema-valid choice.
BR_BALANCE = "tbl_balance00"


def canonical_schema() -> dict[str, Any]:
    """A structurally-valid, referentially-consistent full schema_json document."""
    return {
        "formatVersion": "1.0.0",
        "meta": {"name": "shop", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": [
                {
                    "id": TBL_USERS,
                    "name": "users",
                    "kind": "normal",
                    "domain": "auth",
                    "fields": [
                        {"id": FLD_U_ID, "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                        {"id": FLD_U_EMAIL, "name": "email", "semanticType": "email", "nullable": False},
                        {"id": FLD_U_NAME, "name": "full_name", "semanticType": "string", "nullable": True},
                    ],
                },
                {
                    "id": TBL_ORDERS,
                    "name": "orders",
                    "kind": "normal",
                    "domain": "sales",
                    "fields": [
                        {"id": FLD_O_ID, "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                        {"id": FLD_O_USER, "name": "user_id", "semanticType": "foreign_key", "nullable": False},
                        {"id": FLD_O_TOTAL, "name": "total", "semanticType": "money", "nullable": False},
                        {"id": FLD_O_STATUS, "name": "status", "semanticType": "status", "nullable": False},
                    ],
                },
            ],
            "relations": [
                {
                    "id": REL_ORDER_USER,
                    "name": "belongsTo",
                    "type": "one_to_many",
                    "fromTableId": TBL_ORDERS,
                    "toTableId": TBL_USERS,
                    "foreignKeyFieldId": FLD_O_USER,
                    "onDelete": "cascade",
                }
            ],
            "enums": [
                {
                    "id": ENM_PRIORITY,
                    "name": "priority",
                    "values": [{"value": "low"}, {"value": "high"}],
                }
            ],
        },
        "physical": {
            "indexes": [
                {"id": IDX_U_EMAIL, "tableId": TBL_USERS, "columns": [FLD_U_EMAIL], "unique": True, "type": "btree"}
            ]
        },
        "semantic": {
            "ownership": {TBL_ORDERS: FLD_O_USER},
            "tenancy": {"model": "single"},
            "businessRules": [
                {
                    "id": BR_BALANCE,
                    "category": "invariant",
                    "severity": "error",
                    "intent": "Order total must be non-negative.",
                    "targets": [FLD_O_TOTAL],
                }
            ],
            "stateMachines": [order_state_machine()],
        },
        "presentation": {
            "nodes": [
                {"tableId": TBL_USERS, "x": 0, "y": 0},
                {"tableId": TBL_ORDERS, "x": 320, "y": 0},
            ],
            "viewport": {"zoom": 1.0, "offsetX": 0, "offsetY": 0},
        },
    }


def order_state_machine() -> dict[str, Any]:
    """The reference OrderStatus machine from ``docs/spec-state-machine-designer.md`` §2."""
    return {
        "id": SM_ORDER,
        "name": "OrderStatus",
        "fieldId": FLD_O_STATUS,
        "states": [
            {"id": STT_PENDING, "name": "pending", "initial": True},
            {"id": STT_PAID, "name": "paid"},
            {"id": STT_SHIPPED, "name": "shipped", "final": True},
            {"id": STT_CANCELLED, "name": "cancelled", "final": True},
        ],
        "transitions": [
            {"id": TRN_PAY, "name": "pay", "from": STT_PENDING, "to": STT_PAID, "permission": "orders.pay"},
            {"id": TRN_SHIP, "name": "ship", "from": STT_PAID, "to": STT_SHIPPED, "permission": "orders.ship"},
            {
                "id": TRN_CANCEL,
                "name": "cancel",
                "from": STT_PENDING,
                "to": STT_CANCELLED,
                "permission": "orders.cancel",
                "sideEffects": ["release_stock"],
            },
        ],
    }
