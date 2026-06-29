"""Multi-driver milestone §4 — the target-database selection the ``/designer`` UI drives.

The Code panel sends ``driver`` to ``/design/code`` and the engine returns the matching dialect; the
frameworks list + capabilities advertise both databases. No SQL is generated in the browser.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.module import app

client = TestClient(app)


def _schema() -> dict:
    return {
        "formatVersion": "1.0.0",
        "logical": {
            "tables": [
                {"id": "tbl_users0001", "name": "users", "kind": "normal", "fields": [
                    {"id": "fld_uid000001", "name": "id", "semanticType": "uuid",
                     "isPrimaryKey": True, "nullable": False},
                    {"id": "fld_uactive01", "name": "is_active", "semanticType": "boolean", "nullable": False},
                ]},
            ],
        },
    }


def test_code_endpoint_emits_mysql_dialect_when_driver_is_mysql() -> None:
    res = client.post("/design/code", json={"schema_json": _schema(), "kind": "sql", "driver": "mysql"})
    assert res.status_code == 200
    content = res.json()["content"]
    assert "ENGINE=InnoDB" in content
    assert "`id` char(36) NOT NULL" in content   # uuid → CHAR(36) on MySQL
    assert "tinyint(1)" in content               # boolean → TINYINT(1)


def test_code_endpoint_defaults_to_postgres() -> None:
    res = client.post("/design/code", json={"schema_json": _schema(), "kind": "sql"})
    content = res.json()["content"]
    assert '"id" uuid NOT NULL' in content        # native uuid on Postgres
    assert "ENGINE=InnoDB" not in content


def test_frameworks_and_capabilities_advertise_both_drivers() -> None:
    fw = client.get("/design/code/frameworks").json()
    assert "postgres" in fw["sql"] and "mysql" in fw["sql"]
    caps = client.get("/capabilities").json()
    assert "postgres" in caps["drivers"] and "mysql" in caps["drivers"]
