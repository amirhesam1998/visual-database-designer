"""Reference FastAPI server generator (Milestone 4 §3).

The OpenAPI document is the milestone's product; this generated server is the **proof vehicle** that
makes the live gate real — a runnable thing, not just a document. ``generate_server_files`` emits a
**self-contained, standalone** ``main.py`` (plus ``requirements.txt`` and ``README.md``) that:

* is driven entirely by an embedded compact *contract* (the single source of truth from
  :mod:`app.core.api_contract`) — routes, validation and DB mapping are read from it in a loop, so the
  file is generic and correct regardless of schema;
* serves CRUD under ``/{version}`` against Postgres (psycopg, connected lazily per request);
* validates every request body against the contract **before** touching the database, returning
  RFC 7807 ``application/problem+json`` with per-field ``errors`` on ``422``;
* maps unique violations to ``409``, foreign-key violations to ``409``, missing rows to ``404``;
* enforces state-machine transitions on ``PATCH`` (an illegal status change → ``422``).

The generator is deterministic (the template is fixed; the contract is sorted), so the same schema
yields byte-identical files. Scope (spec §10): one target (FastAPI/Python), one driver (Postgres),
CRUD + light nested reads. The live conformance test ``exec``s the generated ``main.py`` and drives it
with real HTTP against the M3-seeded database — so what is tested is exactly the generated artifact.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.api_contract import build_contract

# The standalone server. ``__CONTRACT_JSON__`` / ``__API_VERSION__`` are substituted by the generator.
# It deliberately imports nothing from this package so the emitted file runs anywhere fastapi+psycopg
# are installed.
_SERVER_TEMPLATE = '''\
"""Auto-generated CRUD API server (Visual Database Designer, Milestone 4). Do not edit by hand.

Run with:  DATABASE_URL=postgresql://... uvicorn main:app
The contract embedded below is the single source of truth (it mirrors the generated OpenAPI document).
"""
from __future__ import annotations

import datetime
import decimal
import json
import os
import re
import uuid as _uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CONTRACT: dict[str, Any] = json.loads(r"""__CONTRACT_JSON__""")
VERSION = "__API_VERSION__"
PREFIX = "/" + VERSION

RES = {r["table"]: r for r in CONTRACT["resources"]}
_EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")

app = FastAPI(title="Generated API", version="1.0.0")


# ----------------------------------------------------------------------------- helpers
def _problem(status: int, title: str, detail: str | None = None, errors: list | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(dsn, row_factory=dict_row)


def _jsonable(v: Any) -> Any:
    if isinstance(v, _uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def _row_out(row: dict) -> dict:
    return {k: _jsonable(v) for k, v in row.items()}


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _validate_value(name: str, val: Any, spec: dict) -> list[dict]:
    t, fmt = spec.get("type"), spec.get("format")
    errs: list[dict] = []
    if t == "string":
        if not isinstance(val, str):
            return [{"field": name, "message": "must be a string"}]
        if fmt == "uuid":
            try:
                _uuid.UUID(val)
            except (ValueError, AttributeError, TypeError):
                errs.append({"field": name, "message": "must be a valid uuid"})
        if fmt == "email" and not _EMAIL_RE.match(val):
            errs.append({"field": name, "message": "must be a valid email"})
        if "enum" in spec and val not in spec["enum"]:
            errs.append({"field": name, "message": "must be one of: " + ", ".join(spec["enum"])})
        if "maxLength" in spec and len(val) > spec["maxLength"]:
            errs.append({"field": name, "message": "exceeds max length " + str(spec["maxLength"])})
        if "pattern" in spec and not re.match(spec["pattern"], val):
            errs.append({"field": name, "message": "does not match required pattern"})
    elif t == "integer":
        if isinstance(val, bool) or not isinstance(val, int):
            errs.append({"field": name, "message": "must be an integer"})
    elif t == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            errs.append({"field": name, "message": "must be a number"})
    elif t == "boolean":
        if not isinstance(val, bool):
            errs.append({"field": name, "message": "must be a boolean"})
    return errs


def _validate(res: dict, data: dict, *, partial: bool) -> list[dict]:
    errors: list[dict] = []
    fields = {f["name"]: f for f in res["fields"]}
    if not partial:
        for f in res["fields"]:
            if f.get("required") and (f["name"] not in data or data[f["name"]] is None):
                errors.append({"field": f["name"], "message": "required"})
    for name, val in data.items():
        f = fields.get(name)
        if f is None:
            errors.append({"field": name, "message": "unknown field"})
            continue
        if f.get("readOnly"):
            errors.append({"field": name, "message": "read-only field cannot be set"})
            continue
        if val is None:
            continue
        errors += _validate_value(name, val, f["openapi"])
    return errors


def _writable(res: dict) -> set:
    return {f["name"] for f in res["fields"] if not f.get("readOnly")}


# ----------------------------------------------------------------------------- route factory
def _register(res: dict) -> None:
    table = res["table"]
    pk = res["pk"]
    writable = _writable(res)
    sm_field = next((f for f in res["fields"] if f.get("stateMachine")), None)

    @app.get(PREFIX + "/" + table)
    def _list(limit: int = 50, offset: int = 0, _res=res):
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM " + _qi(table) + " ORDER BY 1 LIMIT %s OFFSET %s", (limit, offset))
            return [_row_out(r) for r in cur.fetchall()]

    @app.post(PREFIX + "/" + table, status_code=201)
    async def _create(request: Request, _res=res):
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return _problem(422, "Invalid JSON body")
        if not isinstance(data, dict):
            return _problem(422, "Body must be a JSON object")
        errs = _validate(_res, data, partial=False)
        if errs:
            return _problem(422, "Validation failed", errors=errs)
        provided = {k: v for k, v in data.items() if k in writable}
        if _res.get("pkIsUuid") and pk not in provided:
            provided[pk] = str(_uuid.uuid4())
        cols = list(provided.keys())
        col_sql = ", ".join(_qi(c) for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        params = [provided[c] for c in cols]
        sql = "INSERT INTO " + _qi(table) + " (" + col_sql + ") VALUES (" + placeholders + ") RETURNING *"
        return _execute_write(sql, params, status=201)

    @app.get(PREFIX + "/" + table + "/{item_id}")
    def _get(item_id: str, _res=res):
        row = _fetch_one(table, pk, item_id)
        if row is None:
            return _problem(404, _res["model"] + " not found")
        return _row_out(row)

    @app.patch(PREFIX + "/" + table + "/{item_id}")
    async def _update(item_id: str, request: Request, _res=res):
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return _problem(422, "Invalid JSON body")
        if not isinstance(data, dict):
            return _problem(422, "Body must be a JSON object")
        errs = _validate(_res, data, partial=True)
        if errs:
            return _problem(422, "Validation failed", errors=errs)
        current = _fetch_one(table, pk, item_id)
        if current is None:
            return _problem(404, _res["model"] + " not found")
        if sm_field is not None and sm_field["name"] in data:
            new = data[sm_field["name"]]
            cur_val = _jsonable(current.get(sm_field["name"]))
            allowed = [list(t) for t in sm_field["stateMachine"]["transitions"]]
            if new != cur_val and [cur_val, new] not in allowed:
                return _problem(422, "Illegal state transition",
                                detail="cannot move " + str(cur_val) + " -> " + str(new),
                                errors=[{"field": sm_field["name"], "message": "transition not allowed"}])
        updates = {k: v for k, v in data.items() if k in writable}
        if not updates:
            return _row_out(current)
        set_sql = ", ".join(_qi(c) + " = %s" for c in updates)
        params = list(updates.values()) + [item_id]
        sql = "UPDATE " + _qi(table) + " SET " + set_sql + " WHERE " + _qi(pk) + " = %s RETURNING *"
        return _execute_write(sql, params, status=200)

    @app.delete(PREFIX + "/" + table + "/{item_id}", status_code=204)
    def _delete(item_id: str, _res=res):
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM " + _qi(table) + " WHERE " + _qi(pk) + " = %s", (item_id,))
            if cur.rowcount == 0:
                return _problem(404, _res["model"] + " not found")
        return JSONResponse(None, status_code=204)

    for nest in _res.get("nested", []):
        _register_nested(res, nest)


def _register_nested(res: dict, nest: dict) -> None:
    table, pk = res["table"], res["pk"]
    child, fk, path = nest["child"], nest["fk"], nest["path"]

    @app.get(PREFIX + "/" + table + "/{item_id}/" + path)
    def _nested(item_id: str, _res=res):
        if _fetch_one(table, pk, item_id) is None:
            return _problem(404, _res["model"] + " not found")
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM " + _qi(child) + " WHERE " + _qi(fk) + " = %s", (item_id,))
            return [_row_out(r) for r in cur.fetchall()]


def _fetch_one(table: str, pk: str, item_id: str) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM " + _qi(table) + " WHERE " + _qi(pk) + " = %s", (item_id,))
        return cur.fetchone()


def _execute_write(sql: str, params: list, *, status: int) -> JSONResponse:
    import psycopg

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return JSONResponse(_row_out(row), status_code=status)
    except psycopg.errors.UniqueViolation:
        return _problem(409, "Unique constraint violation")
    except psycopg.errors.ForeignKeyViolation:
        return _problem(409, "Foreign-key violation",
                        detail="a referenced row does not exist")
    except psycopg.errors.CheckViolation as exc:
        return _problem(422, "Check constraint violation", detail=str(exc.diag.message_primary or ""))


for _res in CONTRACT["resources"]:
    _register(_res)


@app.get("/health")
def _health():
    return {"status": "ok", "resources": [r["table"] for r in CONTRACT["resources"]]}
'''

_README_TEMPLATE = """\
# Generated API server

Auto-generated by the Visual Database Designer (Milestone 4). The OpenAPI document is the source of
truth; this FastAPI server is derived from the same contract and serves CRUD under `/{version}`.

## Run

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
uvicorn main:app --reload
```

Resources: {resources}

## Notes
- Validation (required / format / enum / foreign-key uuid) runs before the database; failures return
  RFC 7807 `application/problem+json` with per-field `errors` (422).
- Unique/foreign-key violations → 409; missing rows → 404; illegal state transitions on PATCH → 422.
- This server is a reference/proof artifact, not a production deployment.
"""

_REQUIREMENTS = "fastapi==0.115.*\nuvicorn[standard]==0.34.*\npsycopg[binary]==3.3.*\n"


def generate_server_files(schema_json: dict[str, Any], *, version: str = "v1",
                          contract: dict[str, Any] | None = None) -> dict[str, str]:
    """Deterministically generate the standalone FastAPI server files for ``schema_json``."""
    contract = contract or build_contract(schema_json, version=version)
    contract_json = json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=True)
    main_py = _SERVER_TEMPLATE.replace("__CONTRACT_JSON__", contract_json).replace("__API_VERSION__", version)
    resources = ", ".join(r["table"] for r in contract["resources"])
    return {
        "main.py": main_py,
        "requirements.txt": _REQUIREMENTS,
        "README.md": _README_TEMPLATE.format(version=version, resources=resources),
    }
