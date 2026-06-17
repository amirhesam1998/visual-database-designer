"""Unit tests for three-way drift detection + reconcile (Milestone 2 §2)."""

from __future__ import annotations

from app.core import schema_json as sj
from app.core.drift import reconcile, three_way_drift


def _schema(tables: list[dict]) -> sj.SchemaJson:
    return sj.load({"formatVersion": "1.0.0", "logical": {"tables": tables}}, validate=False)


def _table(tid: str, name: str, cols: list[tuple[str, str]]) -> dict:
    fields = []
    for i, (cname, stype) in enumerate(cols):
        f = {"id": f"fld_{tid[-3:]}{i:03d}", "name": cname, "semanticType": stype,
             "nullable": cname != "id"}
        if cname == "id":
            f["isPrimaryKey"] = True
        fields.append(f)
    return {"id": tid, "name": name, "kind": "normal", "fields": fields}


# --- categories (the six scenarios of §2.4) -------------------------------------------------------


def test_synced_produces_no_drift():
    cols = [("id", "uuid"), ("email", "email")]
    a = _schema([_table("tbl_a01", "users", cols)])
    b = _schema([_table("tbl_b01", "users", cols)])
    c = _schema([_table("tbl_c01", "users", cols)])
    report = three_way_drift(a, b, c)
    assert report.drift == [] and report.exit_code == 0


def test_migration_not_applied():
    a = _schema([_table("tbl_a01", "users", [("id", "uuid"), ("phone", "string")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid"), ("phone", "string")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "users.phone")
    assert entry.category == "migration_not_applied"
    assert entry.suggestion == {"action": "apply_migration"}


def test_manual_prod_change_is_error_and_fails_gate():
    a = _schema([_table("tbl_a01", "users", [("id", "uuid")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid"), ("hotfix", "string")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "users.hotfix")
    assert entry.category == "manual_prod_change" and entry.severity == "error"
    assert report.exit_code == 1
    assert entry.suggestion == {"action": "import_to_design"}


def test_design_ahead_of_code():
    a = _schema([_table("tbl_a01", "users", [("id", "uuid")]), _table("tbl_a02", "drafts", [("id", "uuid")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "drafts")
    assert entry.category == "design_ahead_of_code"
    assert entry.suggestion == {"action": "generate_migration"}


def test_code_ahead_of_design():
    a = _schema([_table("tbl_a01", "users", [("id", "uuid")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid")]), _table("tbl_b02", "audit", [("id", "uuid")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid")]), _table("tbl_c02", "audit", [("id", "uuid")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "audit")
    assert entry.category == "code_ahead_of_design"


def test_migration_incomplete_type_inconsistency():
    # Same column everywhere, but the three legs disagree on type and A != B (a half-applied change).
    a = _schema([_table("tbl_a01", "users", [("id", "uuid"), ("age", "integer")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid"), ("age", "big_integer")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid"), ("age", "integer")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "users.age")
    assert entry.category == "migration_incomplete" and entry.kind == "type"


def test_live_type_diverged_is_manual_prod_change():
    # A and B agree; only C's type differs → someone altered prod.
    a = _schema([_table("tbl_a01", "users", [("id", "uuid"), ("age", "integer")])])
    b = _schema([_table("tbl_b01", "users", [("id", "uuid"), ("age", "integer")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid"), ("age", "big_integer")])])
    report = three_way_drift(a, b, c)
    entry = next(d for d in report.drift if d.entity == "users.age")
    assert entry.category == "manual_prod_change" and entry.kind == "type"


# --- reconcile (§2.3) -----------------------------------------------------------------------------


def test_reconcile_matches_by_name_despite_different_ids():
    # A (designed) has its own Stable IDs; the importer-built legs key by name with different ids.
    a = _schema([_table("tbl_DESIGNED", "users", [("id", "uuid")])])
    c = _schema([_table("tbl_imported9", "users", [("id", "uuid")])])
    rec = reconcile(a, None, c)
    assert rec.matched == 1 and rec.ambiguous == []
    assert rec.canonical_ids["users"] == "tbl_DESIGNED"  # designed Stable ID adopted as canonical


def test_reconcile_flags_ambiguous_structural_twin_instead_of_guessing():
    # A.customers has no exact-name twin, but live.clients has the same columns → ambiguous, not auto.
    a = _schema([_table("tbl_a01", "customers", [("id", "uuid"), ("email", "email"), ("name", "string")])])
    c = _schema([_table("tbl_c01", "clients", [("id", "uuid"), ("email", "email"), ("name", "string")])])
    rec = reconcile(a, None, c)
    assert rec.matched == 0
    assert len(rec.ambiguous) == 1
    assert rec.ambiguous[0].entity == "customers" and "clients" in rec.ambiguous[0].candidates


def test_sarif_and_exit_code_projection():
    a = _schema([_table("tbl_a01", "users", [("id", "uuid")])])
    c = _schema([_table("tbl_c01", "users", [("id", "uuid"), ("hotfix", "string")])])
    report = three_way_drift(a, None, c)
    sarif = report.to_sarif()
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["results"][0]["level"] == "error"  # manual_prod_change → error
    assert report.exit_code == 1


def test_designed_foreign_key_does_not_false_positive_against_live_uuid():
    """A designed FK column (semantic 'foreign_key') must not show as type drift against a live
    'uuid' FK column — the shared Type-System FK resolution makes both resolve to uuid (M2 §7)."""
    designed = sj.load({"formatVersion": "1.0.0", "logical": {
        "tables": [
            {"id": "tbl_u", "name": "users", "kind": "normal", "fields": [
                {"id": "fld_uid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False}]},
            {"id": "tbl_o", "name": "orders", "kind": "normal", "fields": [
                {"id": "fld_oid", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                {"id": "fld_ouid", "name": "user_id", "semanticType": "foreign_key", "nullable": False}]},
        ],
        "relations": [{"id": "rel_o", "name": "belongsTo", "type": "one_to_many",
                       "fromTableId": "tbl_o", "toTableId": "tbl_u", "foreignKeyFieldId": "fld_ouid"}],
    }}, validate=False)
    live = sj.load({"formatVersion": "1.0.0", "logical": {
        "tables": [
            {"id": "tbl_u2", "name": "users", "kind": "normal", "fields": [
                {"id": "fld_uid2", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False}]},
            {"id": "tbl_o2", "name": "orders", "kind": "normal", "fields": [
                {"id": "fld_oid2", "name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False},
                {"id": "fld_ouid2", "name": "user_id", "semanticType": "uuid", "nullable": False}]},
        ],
    }}, validate=False)
    report = three_way_drift(designed, None, live)
    assert not any(d.entity == "orders.user_id" for d in report.drift)
