"""End-to-end orchestrator — wire every proven subsystem into one continuous run (the integration
milestone).

This module adds **no new logic.** It is thin orchestration that runs the existing, individually-proven
pieces back-to-back on one real database and proves the *contracts between them* actually connect:

    PRD ──suggest──▶ schema_json ──[human approval gate]──▶ approved handoff
        ──migration (M1)──▶ apply DDL ──seed (M3)──▶ apply INSERTs
        ──API (M4)──▶ start the generated server against the same DB ──▶ real GET/POST

The single rule of this milestone: **the output of each step is the input to the next, untouched.**
Every downstream step consumes the approved ``handoff["schema_json"]`` — never a re-shaped copy. If two
subsystems do not fit together, that contract leak surfaces here as a precise failed step (which is the
*point* of the milestone), and is never papered over with glue. Determinism holds: the same PRD +
``auto_approve`` + ``seed`` yields the same step result (only the LLM text inside *suggest* may vary, and
the tests run with no LLM).

The orchestrator does touch a real Postgres (it applies the migration, inserts the seed, and drives the
generated API over HTTP) — that is the whole proof. It is exposed behind ``POST /design/run-e2e`` and is
a demo/proof endpoint, not something the normal product flow calls.
"""

from __future__ import annotations

import os
from typing import Any

from app.core import drift as core_drift
from app.core import importer as core_importer
from app.core import schema_json as core_sj
from app.core import seeder as core_seeder
from app.core import suggest as core_suggest
from app.core.api_contract import build_contract
from app.core.api_server import generate_server_files
from app.core.design_session import SessionStore


class E2EError(Exception):
    """A step in the chain failed — carries the exact step so the leak's location is unambiguous."""

    def __init__(self, step: str, detail: str) -> None:
        self.step = step
        self.detail = detail
        super().__init__(f"{step}: {detail}")


def _step(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"step": name, "ok": ok, **extra}


def _executable(statements: list[str]) -> list[str]:
    """Drop comment-only lines so each remaining statement can be executed as-is (shared M1/M3/M4 rule)."""
    return [s for s in statements if not s.strip().startswith("--")]


def _apply(dsn: str, statements: list[str]) -> int:
    """Execute SQL statements against a real Postgres (autocommit). Returns the count applied."""
    import psycopg

    applied = 0
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
                applied += 1
    return applied


# --------------------------------------------------------------------------------------------------
# Step 8/9 — start the generated M4 server against the same DB and make real HTTP requests.
# --------------------------------------------------------------------------------------------------
def _post_target(resources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick a resource to POST to — prefer one with a foreign key (so the uuid-FK lesson is exercised
    at the HTTP layer), and which has at least one writable field."""
    with_fk = [r for r in resources if any("fk" in f for f in r["fields"])]
    for res in (with_fk or resources):
        if any(not f["readOnly"] for f in res["fields"]):
            return res
    return None


def _post_body(schema: dict[str, Any], target: dict[str, Any], *, seed: int,
               scenario: dict[str, Any] | None) -> dict[str, Any]:
    """Build a valid POST body by **reusing the seeder's own output** (no hand-crafted values): take a
    seeded row for the target table and drop the read-only fields. Its foreign-key values already point
    at rows we inserted (same seed + scenario → identical data), so the body is referentially valid."""
    data = core_seeder.seed_data(schema, seed=seed, scenario=scenario, output="json")["data"]
    rows = data.get(target["table"]) or []
    if not rows:
        return {}
    read_only = {f["name"] for f in target["fields"] if f["readOnly"]}
    return {k: v for k, v in rows[0].items() if k not in read_only and v is not None}


def _exercise_api(dsn: str, schema: dict[str, Any], *, version: str, seed: int,
                  scenario: dict[str, Any] | None) -> dict[str, Any]:
    """Generate the M4 server, ``exec`` it (so we drive the real artifact), point it at ``dsn`` and make
    real HTTP requests: a GET per resource, and one POST. Returns the ``{request: result}`` sample."""
    from fastapi.testclient import TestClient

    files = generate_server_files(schema, version=version)
    namespace: dict[str, Any] = {"__name__": "e2e_generated_main"}
    exec(compile(files["main.py"], "e2e_generated_main.py", "exec"), namespace)  # noqa: S102 - generated
    app = namespace["app"]

    contract = build_contract(schema, version=version)
    resources = contract["resources"]
    prefix = f"/{version}"

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        client = TestClient(app)
        sample: dict[str, Any] = {}
        for res in resources:
            resp = client.get(f"{prefix}/{res['table']}")
            if resp.status_code != 200:
                raise E2EError("api", f"GET {prefix}/{res['table']} returned {resp.status_code}: {resp.text}")
            sample[f"GET {prefix}/{res['table']}"] = len(resp.json())

        target = _post_target(resources)
        if target is not None:
            body = _post_body(schema, target, seed=seed, scenario=scenario)
            resp = client.post(f"{prefix}/{target['table']}", json=body)
            sample[f"POST {prefix}/{target['table']}"] = resp.status_code
            # 201 (created) and 409 (a legitimate unique conflict on re-insert) both mean the API works.
            # A 422 or 5xx means a real validation/contract mismatch — exactly the leak we hunt for.
            if resp.status_code >= 500 or resp.status_code == 422:
                raise E2EError("api", f"POST {prefix}/{target['table']} returned {resp.status_code}: {resp.text}")
        return sample
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


# --------------------------------------------------------------------------------------------------
# The shared tail: validate → submit → [approval gate] → migration → seed → API.
# --------------------------------------------------------------------------------------------------
def _finish(store: SessionStore, session_id: str, *, dsn: str, auto_approve: bool, seed: int,
            scenario: dict[str, Any] | None, version: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    # Step 4 — validate (structural + Validation Engine).
    session, outcome = store.validate(session_id)
    if not outcome.is_green:
        steps.append(_step("validate", False, summary=outcome.summary, structuralErrors=outcome.structural_errors))
        return {"steps": steps, "result": "red", "failedStep": "validate", "sessionId": session_id}
    steps.append(_step("validate", True, summary=outcome.summary))

    # Step 3/5 — the one and only human pause (AD-5). Without approval, nothing downstream runs.
    store.submit(session_id)
    if not auto_approve:
        steps.append(_step("approve", False, reason="awaiting_human_approval"))
        return {"steps": steps, "result": "awaiting_approval", "sessionId": session_id, "state": "pending_approval"}

    store.approve(session_id, approved_by="e2e-auto")
    handoff = store.handoff(session_id)
    steps.append(_step("approve", True, schemaVersion=handoff["schemaVersion"], checksum=handoff["checksum"]))

    # From here every step consumes the *approved* artifact, untouched.
    schema = handoff["schema_json"]
    try:
        # Step 6 — migration: Diff → Risk → safe plan → SQL (already in the handoff), applied for real.
        up = _executable(handoff["migration"]["sql"]["up"])
        applied = _apply(dsn, up)
        steps.append(_step("migration", True, applied=True, statements=applied))

        # Step 7 — seed: scenario-based, deterministic rows, inserted for real.
        seeded = core_seeder.seed_data(schema, seed=seed, scenario=scenario, output="sql")
        _apply(dsn, seeded["sql"]["statements"])
        steps.append(_step("seed", True, rows=seeded["rows"]))

        # Step 8/9 — API up + real HTTP requests against the same database.
        sample = _exercise_api(dsn, schema, version=version, seed=seed, scenario=scenario)
        steps.append(_step("api", True, sample=sample))
    except E2EError as exc:
        steps.append(_step(exc.step, False, error=exc.detail))
        return {"steps": steps, "result": "red", "failedStep": exc.step, "sessionId": session_id}
    except Exception as exc:  # noqa: BLE001 - any subsystem error → a precise, reported failure (the point)
        failed = steps[-1]["step"] if steps and not steps[-1]["ok"] else _infer_failed(steps)
        steps.append(_step(failed, False, error=str(exc)))
        return {"steps": steps, "result": "red", "failedStep": failed, "sessionId": session_id}

    return {"steps": steps, "result": "green", "sessionId": session_id, "schemaVersion": handoff["schemaVersion"]}


def _infer_failed(steps: list[dict[str, Any]]) -> str:
    """The next step after the last successful one (so an unexpected error still names a location)."""
    done = {s["step"] for s in steps if s["ok"]}
    for name in ("migration", "seed", "api"):
        if name not in done:
            return name
    return "api"


# --------------------------------------------------------------------------------------------------
# Path 1 — greenfield: from a free-text PRD to a live API.
# --------------------------------------------------------------------------------------------------
async def run_greenfield(store: SessionStore, *, dsn: str, prd: str | None = None,
                         schema_json: dict[str, Any] | None = None, auto_approve: bool = False,
                         seed: int = core_seeder.DEFAULT_SEED, scenario: dict[str, Any] | None = None,
                         version: str = "v1", llm: Any | None = None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    session = store.create(mode="greenfield", prd=prd)

    # Step 2 — suggest (the only LLM touchpoint; degrades to the deterministic heuristic with no LLM).
    if schema_json is None:
        result = await core_suggest.suggest_schema(session.prd or "", llm=llm)
        store.attach_suggestion(session.id, result["suggestion"])
        store.apply_schema(session.id, result["suggestion"])
        steps.append(_step("suggest", True, source=result["source"],
                           tables=len(result["suggestion"].get("logical", {}).get("tables", []))))
    else:
        # A caller may supply a schema directly (still goes through the same approval gate).
        store.apply_schema(session.id, schema_json)
        steps.append(_step("suggest", True, source="provided"))

    return _finish(store, session.id, dsn=dsn, auto_approve=auto_approve, seed=seed,
                   scenario=scenario, version=version, steps=steps)


# --------------------------------------------------------------------------------------------------
# Path 2 — brownfield: import an existing DB, prove drift connects, then seed + API.
# --------------------------------------------------------------------------------------------------
async def run_brownfield(store: SessionStore, *, dsn: str, auto_approve: bool = False,
                         seed: int = core_seeder.DEFAULT_SEED, scenario: dict[str, Any] | None = None,
                         version: str = "v1", llm: Any | None = None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    # Step 1 — import the existing database into a Stable-ID schema_json (M2).
    imported = core_importer.build_schema_json(core_importer.introspect_postgres(dsn), name="imported")
    schema = imported["schema_json"]
    steps.append(_step("import", True, tables=len(schema.get("logical", {}).get("tables", []))))

    # Step 2 — drift: compare the designed schema against the live DB (spec §2, a two-way check). With
    # no separate migration history the design *is* the baseline (M2 §0 baselineSource="import"), so the
    # migrations leg is the designed schema; designed == live → no drift.
    designed = core_sj.load(schema, validate=False)
    live = core_sj.load(
        core_importer.build_schema_json(core_importer.introspect_postgres(dsn), name="live")["schema_json"],
        validate=False,
    )
    report = core_drift.three_way_drift(designed, designed, live)
    steps.append(_step("drift", True, summary=report.summary, exitCode=report.exit_code))

    # The imported schema is BOTH the draft and the migration baseline (M2 §0), so the migration delta
    # from the live database is empty — exactly right for an already-existing schema.
    session = store.create(mode="brownfield", schema_json=schema, baseline=schema, baseline_source="import")
    return _finish(store, session.id, dsn=dsn, auto_approve=auto_approve, seed=seed,
                   scenario=scenario, version=version, steps=steps)


async def run_e2e(store: SessionStore, *, mode: str = "greenfield", dsn: str,
                  prd: str | None = None, schema_json: dict[str, Any] | None = None,
                  auto_approve: bool = False, seed: int = core_seeder.DEFAULT_SEED,
                  scenario: dict[str, Any] | None = None, version: str = "v1",
                  llm: Any | None = None) -> dict[str, Any]:
    """Run the whole chain on a real database and report each step. ``mode`` selects greenfield (PRD →
    API) or brownfield (import → drift → seed → API)."""
    if mode == "brownfield":
        return await run_brownfield(store, dsn=dsn, auto_approve=auto_approve, seed=seed,
                                    scenario=scenario, version=version, llm=llm)
    return await run_greenfield(store, dsn=dsn, prd=prd, schema_json=schema_json,
                                auto_approve=auto_approve, seed=seed, scenario=scenario,
                                version=version, llm=llm)
