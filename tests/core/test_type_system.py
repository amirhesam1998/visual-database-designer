"""Conformance kit — Type System (AD-2).

Covers the checklist in ``docs/spec-type-system.md`` §9: per-type consumer snapshot, round-trip
(physical → reverse inference → semantic → physical), override isolation, driver coverage (valid
physical or explicit error), and the resolution pipeline feeding all six consumers from one record.
"""

from __future__ import annotations

import pytest

from app.core.schema_json import Field_, FieldOverrides, PrivacyOverride, ValidationOverride
from app.core.type_system import (
    DEFAULT_REGISTRY,
    PhysicalSpec,
    SemanticTypeDef,
    TypeRegistry,
    UnsupportedPhysicalTypeError,
    build_default_registry,
    infer_semantic_type,
)

R = DEFAULT_REGISTRY


def _field(semantic_type: str, name: str = "f", nullable: bool = True, overrides=None) -> Field_:
    return Field_(id="fld_test00001", name=name, semanticType=semantic_type, nullable=nullable, overrides=overrides)


# --- registry basics ------------------------------------------------------------------------------


def test_base_types_present():
    for tid in ["string", "email", "money", "uuid", "status", "foreign_key", "vector_embedding"]:
        assert R.has(tid)


def test_namespace_core_alias():
    assert R.has("core:email")
    assert R.get("core:money").id == "money"


def test_duplicate_registration_is_a_collision():
    reg = build_default_registry()
    with pytest.raises(ValueError):
        reg.register(R.get("email"))


def test_unknown_type_raises():
    with pytest.raises(KeyError):
        R.get("does_not_exist")


# --- resolution feeds all six consumers from one record (the AD-2 essence) -------------------------


def test_one_record_answers_six_consumers():
    r = R.resolve(_field("email", "email", nullable=False))
    consumers = r.consumers()
    assert consumers["migration"]["type"] == "varchar"
    assert "email" in consumers["validation"]
    assert consumers["form"]["component"] == "email-input"
    assert consumers["openapi"]["format"] == "email"
    assert consumers["seeder"]["generator"] == "email"
    assert consumers["gdpr"] == {"pii": True, "sensitivity": "medium"}
    assert consumers["api"] == {"strategy": "partial", "show": "domain"}


def test_money_resolves_deterministically_per_doc_example():
    r = R.resolve(_field("money", "price", nullable=False), "postgres")
    assert r.physical == {"type": "numeric", "precision": 12, "scale": 2}
    assert r.validation == ["required", "numeric", "min:0"]
    assert r.fake == {"generator": "decimal_in_range", "params": {"min": 0, "max": 100000, "scale": 2}}


def test_nullable_drops_required_rule():
    assert "required" not in R.resolve(_field("string", nullable=True)).validation
    assert "required" in R.resolve(_field("string", nullable=False)).validation


# --- override isolation: an override must not corrupt the rest (spec §9) ---------------------------


def test_physical_override_only_touches_overridden_keys():
    ov = FieldOverrides(physical={"precision": 14})
    r = R.resolve(_field("money", "price", nullable=False, overrides=ov), "postgres")
    assert r.physical == {"type": "numeric", "precision": 14, "scale": 2}  # type/scale preserved


def test_validation_add_remove_override():
    ov = FieldOverrides(validation=ValidationOverride(add=["max:1000000"], remove=["min:0"]))
    r = R.resolve(_field("money", "price", nullable=False, overrides=ov))
    assert "max:1000000" in r.validation and "min:0" not in r.validation


def test_privacy_override():
    ov = FieldOverrides(privacy=PrivacyOverride(pii=True, sensitivity="high"))
    r = R.resolve(_field("string", "secret", overrides=ov))
    assert r.privacy == {"pii": True, "sensitivity": "high"}


def test_override_does_not_mutate_registry_definition():
    before = R.get("money").physical_default.model_dump()
    R.resolve(_field("money", "x", overrides=FieldOverrides(physical={"precision": 99})))
    assert R.get("money").physical_default.model_dump() == before


# --- driver coverage: valid physical or explicit error (spec §8) ----------------------------------


@pytest.mark.parametrize("driver", ["postgres", "mysql", "sqlite"])
def test_every_type_has_physical_or_explicit_error(driver):
    for tid in R.ids():
        definition = R.get(tid)
        if driver in definition.unsupported_on:
            with pytest.raises(UnsupportedPhysicalTypeError):
                definition.physical_for(driver)
        else:
            assert definition.physical_for(driver).type  # non-empty physical type


def test_vector_unsupported_on_mysql_but_ok_on_postgres():
    with pytest.raises(UnsupportedPhysicalTypeError):
        R.resolve(_field("vector_embedding", "emb"), "mysql")
    assert R.resolve(_field("vector_embedding", "emb"), "postgres").physical["type"] == "vector"


def test_json_driver_specific_physical():
    assert R.resolve(_field("json"), "postgres").physical["type"] == "jsonb"
    assert R.resolve(_field("json"), "mysql").physical["type"] == "json"


# --- reverse inference round-trip (spec §9) -------------------------------------------------------


@pytest.mark.parametrize(
    ("physical", "name", "unique", "expected"),
    [
        ("varchar", "email", True, "email"),
        ("varchar", "password", False, "password"),
        ("numeric", "price", False, "money"),
        ("decimal", "ratio", False, "decimal"),
        ("uuid", "id", False, "uuid"),
        ("boolean", "active", False, "boolean"),
        ("timestamp", "created_at", False, "datetime"),
        ("vector(1536)", "emb", False, "vector_embedding"),
        ("bigint", "user_id", False, "big_integer"),
    ],
)
def test_reverse_inference(physical, name, unique, expected):
    assert infer_semantic_type(physical, name, unique=unique).semantic_type == expected


def test_reverse_inference_round_trip_is_stable():
    # physical → semantic → physical should land on a compatible physical family.
    inferred = infer_semantic_type("numeric", "price")
    physical = R.get(inferred.semantic_type).physical_for("postgres")
    assert physical.type in {"numeric", "decimal"}


# --- plugin extension (spec §7) -------------------------------------------------------------------


def test_plugin_can_register_namespaced_type():
    reg = TypeRegistry()
    reg.register(
        SemanticTypeDef(
            id="myteam:iban",
            category="string",
            physical_default=PhysicalSpec(type="varchar", length=34),
            validation_rules=["iban"],
        )
    )
    assert reg.has("myteam:iban")
    assert reg.resolve(_field("myteam:iban", "iban")).physical["length"] == 34
