"""Database-connection milestone (§1/§2) — the import surface the ``/designer`` UI calls.

The visual designer gets two new ways to open an existing database, both thin wrappers over the M2
importer (no SQL parsing in the front-end, spec golden rule):

  * **live**  — ``POST /design/import {dsn}``      → introspect a running Postgres.
  * **file**  — ``POST /design/import {sql}``       → apply a DDL dump to a *shadow* database, then
                                                       introspect it (``VDB_SHADOW_DSN``/``shadowDsn``).

The request-shape contract is tested here without a database (the routing, the 400s); the live half
— that a real Postgres / a real SQL dump round-trips with a uuid FK intact — is the opt-in
``live_postgres`` gate in :mod:`tests.milestones.test_m2_brownfield`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.module import app

client = TestClient(app)


def test_live_import_requires_a_dsn_or_sql() -> None:
    """Neither a live DSN nor a SQL file → a clear 400, never a crash (spec §2)."""
    res = client.post("/design/import", json={})
    assert res.status_code == 400
    assert res.json()["error"] == "missing dsn or sql"


def test_file_import_without_a_shadow_db_is_a_clear_error() -> None:
    """File import needs a shadow database; absent one we say so (not a 500) (spec §2)."""
    res = client.post("/design/import", json={"sql": "CREATE TABLE t (id uuid PRIMARY KEY);"})
    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "shadow_db_unavailable"
    assert "VDB_SHADOW_DSN" in body["detail"]


def test_live_import_bad_dsn_is_reported_not_raised() -> None:
    """A driver/connection failure is surfaced as import_failed 400 (the UI shows it), not a 500."""
    res = client.post("/design/import", json={"dsn": "postgresql://nope:nope@127.0.0.1:1/none"})
    assert res.status_code == 400
    assert res.json()["error"] == "import_failed"


def test_capabilities_advertises_both_import_paths() -> None:
    caps = client.get("/capabilities").json()
    brownfield = " ".join(caps["endpoints"]["brownfield"])
    assert "shadow db" in brownfield  # file import path is advertised
    assert "/design/import" in caps["endpoints"]["designer"]
    assert "/design/drift" in caps["endpoints"]["designer"]
