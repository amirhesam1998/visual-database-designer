"""Unit tests for the brownfield importer's pure build (Milestone 2 §1).

These exercise :func:`build_schema_json` with hand-built :class:`IntrospectedSchema` fixtures — no
database needed. The live introspection + round-trip are proven separately in the conformance kit.
"""

from __future__ import annotations

import json

from app.core import schema_json as sj
from app.core.importer import (
    IntrospectedColumn,
    IntrospectedEnum,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedSchema,
    IntrospectedTable,
    build_schema_json,
)


def _col(name: str, data_type: str, **kw) -> IntrospectedColumn:
    return IntrospectedColumn(name=name, data_type=data_type, **kw)


def _shop() -> IntrospectedSchema:
    """users(id uuid PK, email varchar(255)) ← orders(id uuid PK, user_id uuid FK, total numeric)."""
    return IntrospectedSchema(
        tables=[
            IntrospectedTable(name="users", primary_key=["id"], columns=[
                _col("id", "uuid", nullable=False),
                _col("email", "character varying", char_max_length=255, nullable=False),
            ]),
            IntrospectedTable(name="orders", primary_key=["id"], columns=[
                _col("id", "uuid", nullable=False),
                _col("user_id", "uuid", nullable=False),
                _col("total", "numeric", numeric_precision=12, numeric_scale=2, nullable=False),
            ]),
        ],
        foreign_keys=[IntrospectedForeignKey(
            name="orders_user_id_fkey", table="orders", columns=["user_id"],
            ref_table="users", ref_columns=["id"], on_delete="cascade")],
        indexes=[IntrospectedIndex(name="users_email_key", table="users", columns=["email"], unique=True)],
    )


def test_build_is_structurally_valid():
    out = build_schema_json(_shop())
    assert out["validation"]["structuralErrors"] == []
    # Loads cleanly as a SchemaJson and round-trips through validation with no *errors*.
    sj.load(out["schema_json"])  # raises on structural failure
    assert out["validation"]["summary"]["error"] == 0


def test_build_is_byte_identical_across_runs():
    a = build_schema_json(_shop())["schema_json"]
    b = build_schema_json(_shop())["schema_json"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_uuid_pk_is_preserved_not_coerced_to_bigint():
    schema = sj.load(build_schema_json(_shop())["schema_json"])
    users = next(t for t in schema.logical.tables if t.name == "users")
    pk = users.primary_keys()[0]
    assert pk.semantic_type == "uuid" and pk.is_primary_key


def test_unique_email_varchar_infers_email():
    schema = sj.load(build_schema_json(_shop())["schema_json"])
    users = next(t for t in schema.logical.tables if t.name == "users")
    email = next(f for f in users.fields if f.name == "email")
    assert email.semantic_type == "email"


def test_money_inferred_from_numeric_and_name():
    schema = sj.load(build_schema_json(_shop())["schema_json"])
    orders = next(t for t in schema.logical.tables if t.name == "orders")
    total = next(f for f in orders.fields if f.name == "total")
    assert total.semantic_type == "money"


def test_fk_column_type_read_from_db_is_structurally_correct():
    """The inverse of the bug M1 caught: the FK column's type is read straight from the DB (uuid),
    so it matches the referenced PK by construction — no override needed."""
    schema = sj.load(build_schema_json(_shop())["schema_json"])
    orders = next(t for t in schema.logical.tables if t.name == "orders")
    user_id = next(f for f in orders.fields if f.name == "user_id")
    assert user_id.semantic_type == "uuid"  # the real physical type, preserved


def test_relations_rebuilt_from_foreign_keys():
    schema = sj.load(build_schema_json(_shop())["schema_json"])
    assert len(schema.logical.relations) == 1
    rel = schema.logical.relations[0]
    orders = next(t for t in schema.logical.tables if t.name == "orders")
    users = next(t for t in schema.logical.tables if t.name == "users")
    assert rel.from_table_id == orders.id and rel.to_table_id == users.id
    assert rel.on_delete == "cascade" and rel.type == "one_to_many"


def test_serial_identity_becomes_autoincrement():
    intro = IntrospectedSchema(tables=[IntrospectedTable(name="logs", primary_key=["id"], columns=[
        _col("id", "bigint", nullable=False, default="nextval('logs_id_seq'::regclass)"),
        _col("msg", "text"),
    ])])
    schema = sj.load(build_schema_json(intro)["schema_json"])
    pk = schema.logical.tables[0].primary_keys()[0]
    assert pk.auto_increment and pk.semantic_type == "big_integer"


def test_unique_fk_column_is_one_to_one():
    intro = IntrospectedSchema(
        tables=[
            IntrospectedTable(name="users", primary_key=["id"], columns=[_col("id", "uuid", nullable=False)]),
            IntrospectedTable(name="profiles", primary_key=["id"], columns=[
                _col("id", "uuid", nullable=False),
                _col("user_id", "uuid", nullable=False),
            ]),
        ],
        foreign_keys=[IntrospectedForeignKey(name="profiles_user_id_fkey", table="profiles",
                      columns=["user_id"], ref_table="users", ref_columns=["id"])],
        indexes=[IntrospectedIndex(name="profiles_user_id_key", table="profiles",
                 columns=["user_id"], unique=True)],
    )
    schema = sj.load(build_schema_json(intro)["schema_json"])
    assert schema.logical.relations[0].type == "one_to_one"


def test_pivot_table_is_detected_and_suggests_many_to_many():
    intro = IntrospectedSchema(
        tables=[
            IntrospectedTable(name="users", primary_key=["id"], columns=[_col("id", "uuid", nullable=False)]),
            IntrospectedTable(name="roles", primary_key=["id"], columns=[_col("id", "uuid", nullable=False)]),
            IntrospectedTable(name="role_user", primary_key=["user_id", "role_id"], columns=[
                _col("user_id", "uuid", nullable=False),
                _col("role_id", "uuid", nullable=False),
            ]),
        ],
        foreign_keys=[
            IntrospectedForeignKey(name="ru_user_fkey", table="role_user", columns=["user_id"],
                                   ref_table="users", ref_columns=["id"]),
            IntrospectedForeignKey(name="ru_role_fkey", table="role_user", columns=["role_id"],
                                   ref_table="roles", ref_columns=["id"]),
        ],
    )
    out = build_schema_json(intro)
    schema = sj.load(out["schema_json"])
    pivot = next(t for t in schema.logical.tables if t.name == "role_user")
    assert pivot.kind == "pivot"
    assert any(r.type == "many_to_many" for r in schema.logical.relations)
    assert any(s.get("suggestedType") == "many_to_many" for s in out["inference"]["suggestions"])


def test_table_without_primary_key_is_a_warning_not_a_crash():
    intro = IntrospectedSchema(tables=[IntrospectedTable(name="events", columns=[
        _col("name", "character varying", char_max_length=120),
    ])])
    out = build_schema_json(intro)
    assert out["validation"]["summary"]["error"] == 0
    assert out["validation"]["summary"]["warning"] >= 1  # QLT001: no primary key


def test_real_physical_type_is_preserved_via_override():
    intro = IntrospectedSchema(tables=[IntrospectedTable(name="t", primary_key=["id"], columns=[
        _col("id", "uuid", nullable=False),
        _col("code", "character varying", char_max_length=100),  # not the 255 default for 'string'
    ])])
    schema = sj.load(build_schema_json(intro)["schema_json"])
    code = next(f for f in schema.logical.tables[0].fields if f.name == "code")
    assert code.overrides is not None and code.overrides.physical == {"type": "varchar", "length": 100}


def test_ambiguous_text_column_is_flagged_for_confirmation():
    intro = IntrospectedSchema(tables=[IntrospectedTable(name="docs", primary_key=["id"], columns=[
        _col("id", "uuid", nullable=False),
        _col("body", "text"),
    ])])
    out = build_schema_json(intro)
    assert out["inference"]["ambiguous"] >= 1
    assert any(s.get("column") == "body" for s in out["inference"]["suggestions"])


def test_postgres_enum_becomes_enum_field():
    intro = IntrospectedSchema(
        tables=[IntrospectedTable(name="orders", primary_key=["id"], columns=[
            IntrospectedColumn(name="id", data_type="uuid", nullable=False),
            IntrospectedColumn(name="status", data_type="USER-DEFINED", udt_name="order_status", nullable=False),
        ])],
        enums=[IntrospectedEnum(name="order_status", labels=["pending", "paid", "shipped"])],
    )
    schema = sj.load(build_schema_json(intro)["schema_json"])
    status = next(f for f in schema.logical.tables[0].fields if f.name == "status")
    assert status.semantic_type == "enum" and status.enum_id is not None
    assert schema.enum_by_id(status.enum_id).values[0].value == "pending"
