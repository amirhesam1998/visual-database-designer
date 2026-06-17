"""Conformance kit — schema_json Core + Stable IDs (AD-1, AD-3).

Mirrors the test checklist in ``docs/spec-schema-json-format.md`` §11 and ``docs/spec-type-system``
AD-1 notes: meta-schema is valid, valid fixtures pass, invalid fixtures fail with the expected
error, round-trip is stable, and format migration upgrades a v0 file to a v1-valid one.
"""

from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from app.core import ids
from app.core import schema_json as sj

from .factory import FLD_U_ID, TBL_USERS, canonical_schema

# --- Stable IDs (AD-1) ----------------------------------------------------------------------------


def test_meta_schema_is_itself_valid():
    Draft202012Validator.check_schema(sj._meta_schema())


@pytest.mark.parametrize("prefix", sorted(ids.PREFIXES))
def test_new_id_is_valid_and_typed(prefix):
    new = ids.new_id(prefix)
    assert ids.is_valid_id(new)
    assert ids.id_prefix(new) == prefix
    assert ids.id_type(new) == ids.PREFIXES[prefix]


def test_ulid_encoding_is_monotonic_in_time():
    # The 10-char time prefix encodes the millisecond clock big-endian, so a later timestamp always
    # sorts lexicographically after an earlier one (the ULID ordering guarantee).
    earlier = ids._encode_crockford(1_000, 10)
    later = ids._encode_crockford(2_000, 10)
    assert later > earlier
    assert len(ids.generate_ulid()) == 26


def test_unknown_prefix_rejected():
    with pytest.raises(ValueError):
        ids.new_id("xyz")


@pytest.mark.parametrize("bad", ["", "tbl_", "tbl_ab", "foo_01234", "01234", None, 123])
def test_invalid_ids_rejected(bad):
    assert not ids.is_valid_id(bad)


# --- Valid fixtures pass --------------------------------------------------------------------------


def test_canonical_schema_is_structurally_valid():
    assert sj.validate_structure(canonical_schema()) == []


def test_load_parses_all_layers():
    s = sj.load(canonical_schema())
    assert s.format_version == "1.0.0"
    assert {t.name for t in s.logical.tables} == {"users", "orders"}
    assert s.physical and s.physical.indexes[0].unique is True
    assert s.semantic and s.semantic.state_machines[0].name == "OrderStatus"
    assert s.presentation and len(s.presentation.nodes) == 2


def test_minimal_schema_only_requires_format_and_logical():
    doc = {"formatVersion": "1.0.0", "logical": {"tables": [
        {"id": TBL_USERS, "name": "users", "fields": [
            {"id": FLD_U_ID, "name": "id", "semanticType": "uuid"}]}]}}
    assert sj.validate_structure(doc) == []


# --- Invalid fixtures fail with the expected error (negative tests) -------------------------------


def test_missing_required_logical_rejected():
    with pytest.raises(sj.SchemaStructuralError) as exc:
        sj.load({"formatVersion": "1.0.0"}, validate=True)
    assert any("logical" in e for e in exc.value.errors)


def test_bad_format_version_pattern_rejected():
    doc = canonical_schema()
    doc["formatVersion"] = "v1"
    errors = sj.validate_structure(doc)
    assert any("formatVersion" in e for e in errors)


def test_table_without_fields_rejected():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["fields"] = []
    errors = sj.validate_structure(doc)
    assert any("fields" in e for e in errors)


def test_unknown_property_rejected():
    doc = canonical_schema()
    doc["logical"]["tables"][0]["bogus"] = 1
    errors = sj.validate_structure(doc)
    assert any("bogus" in e or "Additional" in e for e in errors)


def test_relation_enum_constraint_rejected():
    doc = canonical_schema()
    doc["logical"]["relations"][0]["onDelete"] = "explode"
    errors = sj.validate_structure(doc)
    assert any("onDelete" in e or "explode" in e for e in errors)


# --- Round-trip stability -------------------------------------------------------------------------


def test_round_trip_is_stable():
    s = sj.load(canonical_schema())
    once = sj.load(sj.dump(s))
    twice = sj.load(sj.dump(once))
    assert s == once == twice


def test_dump_output_revalidates():
    s = sj.load(canonical_schema())
    assert sj.validate_structure(sj.dump(s)) == []


# --- Format migration (v0 → v1) -------------------------------------------------------------------


def test_v0_root_tables_migrates_to_logical():
    v0 = {"tables": [{"id": TBL_USERS, "name": "users", "fields": [
        {"id": FLD_U_ID, "name": "id", "semanticType": "uuid"}]}]}
    migrated = sj.migrate(copy.deepcopy(v0))
    assert migrated["formatVersion"] == sj.CURRENT_FORMAT_VERSION
    assert "tables" not in migrated and migrated["logical"]["tables"][0]["name"] == "users"
    # And the migrated doc is now structurally valid.
    assert sj.validate_structure(migrated) == []


def test_already_current_is_noop():
    doc = canonical_schema()
    assert sj.migrate(copy.deepcopy(doc)) == doc
