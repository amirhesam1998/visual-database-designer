"""Scenario-Based Seeder — fills a real schema with valid, insertable data (Milestone 3).

The third proven downstream consumer of an approved schema (after migration and import). It closes the
"real, usable schema" loop: M1 builds the database, M2 reads it, the seeder populates it — and, like
both milestones, it has a real live gate: the generated rows must ``INSERT`` into a live Postgres
**without violating a single constraint** (FK, unique, NOT NULL, enum). A snapshot cannot prove that.

Design (AD-4 / AD-5):

* **Deterministic core.** Value construction, FK/unique/nullable/enum respect, topological order and
  reproducibility are all deterministic and need no LLM. A numeric ``seed`` → byte-identical data in
  every run and process (hash-derived per-cell RNG, explicit sort keys everywhere — the M1 lesson).
* **Types from the Type System.** Each value comes from a field's *resolved* semantic type's
  ``fakeData`` generator, never the raw physical type. A foreign key's value is a *real* primary-key
  value of an already-generated referenced row (resolved through :func:`resolve_fk_physical`), so
  ``orders.user_id`` is a valid ``uuid`` pointing at an existing ``users.id`` — never a bare integer
  or an unrelated uuid (the bug M1/M2 taught us to prove).
* **State-machine consistency.** A status column bound to a state machine only ever gets a *reachable*
  state (via :func:`state_machine.seeder_plan`), and scenarios can require consistent dependent rows
  (a delivered order has a successful payment).
* **Scenario-based, not random.** Instead of "N random rows" the seeder translates a declarative
  scenario (counts + status distributions + derive rules) into schema-, FK- and state-consistent rows.
* **LLM is optional enrichment only** (text realism); structure and relations are always deterministic.

Nothing here applies anything to a database — it produces data + SQL; running it is an explicit step.
"""

from __future__ import annotations

import datetime
import hashlib
import random
from typing import Any

from app.core import schema_json as core_sj
from app.core import state_machine as core_sm
from app.core.schema_json import Field_, SchemaJson, Table
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, UnsupportedPhysicalTypeError, resolve_fk_physical

DEFAULT_SEED = 1337
SUPPORTED_DRIVERS = ("postgres",)


class SeedError(Exception):
    """The schema cannot be seeded as-is (e.g. a NOT NULL foreign-key cycle with no initial data)."""


# Small deterministic word pools (no external faker dependency; reproducible by construction).
_WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "grove", "harbor", "ivory", "jade",
          "koala", "lumen", "maple", "nimbus", "onyx", "petal", "quartz", "river", "sable", "tide"]
_FIRST = ["ava", "ben", "cleo", "dana", "eli", "faye", "gus", "hana", "ivan", "june",
          "kai", "lena", "milo", "nora", "omar", "pia", "quinn", "rosa", "sam", "tara"]
_LAST = ["adams", "blake", "cohen", "dixon", "evans", "frost", "gomez", "hayes", "irving", "jones"]


# ==================================================================================================
# Deterministic per-cell randomness (hash-derived — order-independent, process-independent).
# ==================================================================================================
def _rng(seed: int, *parts: Any) -> random.Random:
    raw = "|".join(str(p) for p in (seed, *parts))
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _uuid(rng: random.Random) -> str:
    return str(__import__("uuid").UUID(int=rng.getrandbits(128), version=4))


def _iran_national_code(rng: random.Random) -> str:
    """A checksum-valid Iranian national code (10 digits) — deterministic for a given rng."""
    digits = [rng.randint(0, 9) for _ in range(9)]
    s = sum(d * (10 - i) for i, d in enumerate(digits)) % 11
    check = s if s < 2 else 11 - s
    return "".join(str(d) for d in digits) + str(check)


# ==================================================================================================
# Value generation from the Type System's fake generators.
# ==================================================================================================
def _generate_scalar(generator: str, params: dict[str, Any], rng: random.Random, idx: int) -> Any:
    """Produce one JSON-able value for a semantic fake generator (deterministic given ``rng``)."""
    if generator == "email":
        return f"{_FIRST[idx % len(_FIRST)]}.{_LAST[rng.randrange(len(_LAST))]}{idx}@example.com"
    if generator == "slug":
        return f"{_WORDS[rng.randrange(len(_WORDS))]}-{idx}"
    if generator == "password":
        return "$2y$10$" + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(53))
    if generator == "phone_ir":
        return "09" + "".join(str(rng.randint(0, 9)) for _ in range(9))
    if generator == "national_code_ir":
        return _iran_national_code(rng)
    if generator == "number":
        return rng.randint(1, 1000)
    if generator in {"decimal", "decimal_in_range"}:
        lo = float(params.get("min", 0))
        hi = float(params.get("max", 1000))
        scale = int(params.get("scale", 2))
        return round(rng.uniform(lo, hi), scale)
    if generator == "boolean":
        return rng.random() < 0.5
    if generator == "date":
        base = datetime.date(2020, 1, 1) + datetime.timedelta(days=rng.randint(0, 2000))
        return base.isoformat()
    if generator in {"datetime", "timestamp"}:
        base = datetime.datetime(2020, 1, 1) + datetime.timedelta(seconds=rng.randint(0, 100_000_000))
        return base.isoformat(sep=" ")
    if generator == "vector":
        dim = int(params.get("dimension", 3))
        return [round(rng.uniform(-1, 1), 4) for _ in range(dim)]
    # "word" and any unknown generator → a short, human-ish token.
    return f"{_WORDS[rng.randrange(len(_WORDS))]} {_LAST[rng.randrange(len(_LAST))]}"


def _value_for_physical(physical: dict[str, Any], rng: random.Random, idx: int) -> Any:
    """Fallback when a field has no usable semantic generator: derive a value from its physical type."""
    t = str(physical.get("type", "text")).lower()
    if t == "uuid":
        return _uuid(rng)
    if t in {"integer", "bigint", "smallint", "serial", "bigserial"}:
        return rng.randint(1, 1000)
    if t in {"numeric", "decimal", "double precision", "real"}:
        return round(rng.uniform(0, 1000), physical.get("scale", 2) or 2)
    if t == "boolean":
        return rng.random() < 0.5
    if t in {"timestamp", "datetime"}:
        moment = datetime.datetime(2020, 1, 1) + datetime.timedelta(seconds=rng.randint(0, 1_000_000))
        return moment.isoformat(sep=" ")
    if t == "date":
        return datetime.date(2020, 1, 1).isoformat()
    if t in {"json", "jsonb"}:
        return {}
    return f"{_WORDS[idx % len(_WORDS)]}{idx}"


# ==================================================================================================
# Schema indexing helpers.
# ==================================================================================================
def _unique_field_ids(schema: SchemaJson) -> set[str]:
    """Field ids that must be unique: primary keys + single-column unique indexes."""
    uniq: set[str] = set()
    for t in schema.logical.tables:
        for f in t.fields:
            if f.is_primary_key:
                uniq.add(f.id)
    if schema.physical:
        for idx in schema.physical.indexes:
            if idx.unique and len(idx.columns) == 1:
                uniq.add(idx.columns[0])
    return uniq


def _fk_index(schema: SchemaJson) -> dict[str, tuple[str, str]]:
    """field_id → (referenced_table_id, relation_id) for every foreign-key field."""
    out: dict[str, tuple[str, str]] = {}
    for rel in schema.logical.relations:
        if rel.foreign_key_field_id and rel.to_table_id:
            out[rel.foreign_key_field_id] = (rel.to_table_id, rel.id)
    return out


def _state_machines_by_field(schema: SchemaJson) -> dict[str, Any]:
    if not schema.semantic:
        return {}
    return {sm.field_id: sm for sm in schema.semantic.state_machines}


def topological_order(schema: SchemaJson) -> list[Table]:
    """Order tables so an FK's referenced table is seeded first (spec §3.1).

    A NOT NULL foreign-key cycle is unseedable → :class:`SeedError`. A *nullable* cycle is broken
    deterministically (the nullable edge is dropped; those columns start NULL).
    """
    by_id = {t.id: t for t in schema.logical.tables}
    field_by_id = {f.id: f for t in schema.logical.tables for f in t.fields}
    deps: dict[str, set[str]] = {tid: set() for tid in by_id}
    nullable_edge: dict[tuple[str, str], bool] = {}
    for rel in schema.logical.relations:
        a, b = rel.from_table_id, rel.to_table_id
        if a not in by_id or b not in by_id or a == b:
            continue  # self-references are resolved against earlier rows of the same table
        deps[a].add(b)
        fk = field_by_id.get(rel.foreign_key_field_id) if rel.foreign_key_field_id else None
        is_nullable = fk.nullable if fk else True
        key = (a, b)
        nullable_edge[key] = nullable_edge.get(key, True) and is_nullable

    order: list[Table] = []
    resolved: set[str] = set()
    remaining = set(by_id)
    while remaining:
        ready = sorted((tid for tid in remaining if deps[tid] <= resolved), key=lambda x: by_id[x].name)
        if not ready:
            dropped = False
            for tid in sorted(remaining, key=lambda x: by_id[x].name):
                for dep in sorted(deps[tid] - resolved, key=lambda x: by_id[x].name):
                    if nullable_edge.get((tid, dep), False):
                        deps[tid].discard(dep)
                        dropped = True
                        break
                if dropped:
                    break
            if not dropped:
                raise SeedError(
                    "foreign-key cycle with NOT NULL columns cannot be seeded without initial data"
                )
            continue
        for tid in ready:
            order.append(by_id[tid])
            resolved.add(tid)
            remaining.discard(tid)
    return order


# ==================================================================================================
# Allowed values for enum / status columns.
# ==================================================================================================
def _allowed_states(field: Field_, schema: SchemaJson, sm_by_field: dict[str, Any]) -> list[str] | None:
    """Allowed values for a choice column: reachable state-machine states, or enum values, else None."""
    sm = sm_by_field.get(field.id)
    if sm is not None:
        reachable = list(core_sm.seeder_plan(sm).keys())  # only states reachable from initial (§3.4)
        if reachable:
            return reachable
    if field.enum_id:
        enum = schema.enum_by_id(field.enum_id)
        if enum:
            return [v.value for v in enum.values]
    return None


# ==================================================================================================
# Scenario resolution (declarative → counts + status distributions + derive rules).
# ==================================================================================================
def _preset(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Named scenario presets. They are *suggestive*: tables/columns the schema lacks are ignored."""
    p = params or {}
    if name in {"ecommerce_medium", "ecommerce"}:
        return {
            "counts": {"tenants": p.get("tenants", 1), "users": p.get("users", 3),
                       "products": p.get("products", 5), "orders": p.get("orders", 10),
                       "categories": p.get("categories", 3)},
            "status": {"orders": {"field": "status",
                                  "distribution": {"delivered": 6, "pending": 2, "cancelled": 2}}},
            "derive": [{"table": "payments", "fromTable": "orders",
                        "where": {"status": "delivered"}, "set": {"status": "success"}}],
        }
    if name in {"multi_tenant", "multitenant"}:
        return {
            "counts": {"tenants": p.get("tenants", 2), "users": p.get("users", 6),
                       "projects": p.get("projects", 8)},
            "status": {}, "derive": [],
        }
    if name == "ticketing":
        return {
            "counts": {"users": p.get("users", 5), "tickets": p.get("tickets", 12)},
            "status": {"tickets": {"field": "status",
                                   "distribution": {"open": 5, "in_progress": 4, "closed": 3}}},
            "derive": [],
        }
    return {"counts": dict(p), "status": {}, "derive": []}


def resolve_scenario(scenario: dict[str, Any] | None, schema: SchemaJson
                     ) -> tuple[dict[str, int], dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Turn a (possibly named) scenario into per-table counts, status distributions and derive rules.

    Counts default to a small number for every table; named/explicit scenarios override. Anything that
    references a table/column the schema does not have is dropped with a warning (graceful, never raises).
    """
    warnings: list[str] = []
    table_names = {t.name for t in schema.logical.tables}
    scenario = scenario or {}
    base = _preset(scenario.get("name", ""), scenario.get("params", {})) if scenario.get("name") else \
        {"counts": dict(scenario.get("params", {})), "status": {}, "derive": []}

    # Explicit overrides on the scenario take precedence over the preset.
    counts_in = {**base["counts"], **(scenario.get("counts", {}))}
    status_in = {**base["status"], **(scenario.get("status", {}))}
    derive_in = scenario.get("derive", base["derive"])

    derived_tables = {d["table"] for d in derive_in if d.get("table") in table_names}
    counts: dict[str, int] = {}
    for t in schema.logical.tables:
        if t.name in derived_tables:
            continue  # a derived table's row count comes from its parent matches, not a fixed count
        counts[t.name] = max(1, int(counts_in.get(t.name, 5)))

    status: dict[str, dict[str, Any]] = {}
    for tname, spec in status_in.items():
        if tname in table_names:
            status[tname] = spec
        else:
            warnings.append(f"scenario status for unknown table '{tname}' ignored")

    derive: list[dict[str, Any]] = []
    for d in derive_in:
        if d.get("table") in table_names and d.get("fromTable") in table_names:
            derive.append(d)
        else:
            warnings.append(f"scenario derive {d.get('table')}←{d.get('fromTable')} ignored (unknown table)")
    return counts, status, derive, warnings


# ==================================================================================================
# The seeder.
# ==================================================================================================
def seed_data(schema_json: dict[str, Any], *, seed: int = DEFAULT_SEED,
              scenario: dict[str, Any] | None = None, output: str = "sql",
              driver: str = "postgres", registry: TypeRegistry | None = None) -> dict[str, Any]:
    """Generate scenario-consistent rows for ``schema_json`` and return ``{rows, sql|data, warnings}``."""
    if driver not in SUPPORTED_DRIVERS:
        raise ValueError(f"seeder supports {SUPPORTED_DRIVERS} for Milestone 3, not {driver!r}")
    reg = registry or DEFAULT_REGISTRY
    schema = core_sj.load(core_sj.migrate(schema_json), validate=False)

    counts, status_dist, derive_rules, warnings = resolve_scenario(scenario, schema)
    order = topological_order(schema)  # raises SeedError on a NOT NULL FK cycle

    fk_index = _fk_index(schema)
    fk_phys = resolve_fk_physical(schema, driver, reg)
    unique_ids = _unique_field_ids(schema)
    sm_by_field = _state_machines_by_field(schema)
    derive_by_table = {d["table"]: d for d in derive_rules}
    by_name = {t.name: t for t in schema.logical.tables}

    generated: dict[str, list[dict[str, Any]]] = {}     # table.id → row dicts (field name → value)
    pk_pool: dict[str, list[Any]] = {}                  # table.id → list of single-PK values for FK refs
    used_unique: dict[tuple[str, str], set[Any]] = {}
    rows_summary: dict[str, int] = {}

    for table in order:
        derive = derive_by_table.get(table.name)
        rows = _seed_table(
            table, schema, seed=seed, count=counts.get(table.name, 0), derive=derive, by_name=by_name,
            generated=generated, pk_pool=pk_pool, fk_index=fk_index, fk_phys=fk_phys,
            unique_ids=unique_ids, used_unique=used_unique, status_dist=status_dist.get(table.name),
            sm_by_field=sm_by_field, reg=reg, warnings=warnings,
        )
        generated[table.id] = rows
        pk_pool[table.id] = _collect_pks(table, rows)
        rows_summary[table.name] = len(rows)

    result: dict[str, Any] = {"rows": rows_summary, "warnings": warnings}
    if output == "json":
        result["data"] = {t.name: generated[t.id] for t in order}
    else:
        statements = _render_sql(order, generated, driver)
        result["sql"] = {"driver": driver, "statements": statements}
    return result


def _seed_table(table: Table, schema: SchemaJson, *, seed: int, count: int, derive: dict | None,
                by_name: dict[str, Table], generated: dict[str, list[dict]], pk_pool: dict[str, list],
                fk_index: dict[str, tuple[str, str]], fk_phys: dict[str, dict], unique_ids: set[str],
                used_unique: dict[tuple[str, str], set], status_dist: dict | None,
                sm_by_field: dict[str, Any], reg: TypeRegistry, warnings: list[str]) -> list[dict[str, Any]]:
    # Derived tables emit one row per matching parent row (spec §4 cross-consistency).
    parents: list[tuple[dict, Any]] = []  # (parent_row, parent_pk) when deriving
    if derive is not None:
        parent = by_name[derive["fromTable"]]
        where = derive.get("where", {})
        parent_pk_name = _single_pk_name(parent)
        for prow in generated.get(parent.id, []):
            if all(prow.get(k) == v for k, v in where.items()):
                parents.append((prow, prow.get(parent_pk_name) if parent_pk_name else None))
        count = len(parents)
        derive_fk_field_id = _fk_field_to(table, parent, schema)
    else:
        derive_fk_field_id = None

    status_plan = _status_plan(table, schema, status_dist, count, seed, sm_by_field, warnings)
    rows: list[dict[str, Any]] = []
    for i in range(count):
        row: dict[str, Any] = {}
        own_pks_so_far = [r for r in rows]  # for self-referencing FKs
        for field in table.fields:
            value = _value_for_column(
                field=field, table=table, schema=schema, seed=seed, idx=i,
                fk_index=fk_index, fk_phys=fk_phys, pk_pool=pk_pool, generated=generated,
                own_rows=own_pks_so_far, unique_ids=unique_ids, used_unique=used_unique,
                sm_by_field=sm_by_field, status_for_row=status_plan[i] if status_plan else None,
                reg=reg, warnings=warnings,
            )
            row[field.name] = value
        # Derive overrides: point the FK at the matched parent and apply the scenario's `set` values.
        if derive is not None:
            _prow, ppk = parents[i]
            if derive_fk_field_id:
                fk_field = next((f for f in table.fields if f.id == derive_fk_field_id), None)
                if fk_field is not None and ppk is not None:
                    row[fk_field.name] = ppk
            for k, v in derive.get("set", {}).items():
                if any(f.name == k for f in table.fields):
                    row[k] = v
        rows.append(row)
    return rows


def _value_for_column(*, field: Field_, table: Table, schema: SchemaJson, seed: int, idx: int,
                      fk_index: dict[str, tuple[str, str]], fk_phys: dict[str, dict],
                      pk_pool: dict[str, list], generated: dict[str, list[dict]], own_rows: list[dict],
                      unique_ids: set[str], used_unique: dict[tuple[str, str], set],
                      sm_by_field: dict[str, Any], status_for_row: Any, reg: TypeRegistry,
                      warnings: list[str]) -> Any:
    rng = _rng(seed, table.name, field.name, idx)

    # 1. Foreign key → a REAL primary-key value of the referenced table (resolve_fk_physical proves
    #    the type matches; the value is an existing referenced row's PK, never a random one).
    if field.id in fk_index:
        ref_table_id, _rel_id = fk_index[field.id]
        if ref_table_id == table.id:  # self-reference: point at an earlier row, else NULL
            pks = [r.get(_single_pk_name(table)) for r in own_rows]
        else:
            pks = pk_pool.get(ref_table_id, [])
        if pks:
            return _rng(seed, table.name, field.name, idx, "fk").choice(pks)
        if not field.nullable:
            warnings.append(f"{table.name}.{field.name}: NOT NULL FK has no referenced rows; left NULL")
        return None

    # 2. Choice column (enum / state-machine status) → a reachable / allowed value.
    allowed = _allowed_states(field, schema, sm_by_field)
    if allowed is not None:
        if status_for_row is not None and status_for_row in allowed:
            return status_for_row
        return rng.choice(allowed)

    # 3. Otherwise generate from the resolved semantic type's fake generator.
    try:
        resolved = reg.resolve(field, "postgres")
        physical = resolved.physical
        gen = resolved.fake.get("generator", "word")
        params = resolved.fake.get("params", {})
    except (KeyError, UnsupportedPhysicalTypeError):
        physical, gen, params = ({"type": "text"}, "word", {})

    value = _uuid(rng) if physical.get("type") == "uuid" else _generate_scalar(gen, params, rng, idx)
    # Auto-increment / integer primary keys get a clean sequential value (also a fine FK target).
    if field.is_primary_key and str(physical.get("type")) in {"integer", "bigint", "smallint"}:
        value = idx + 1
    # Respect varchar length.
    if isinstance(value, str) and physical.get("length"):
        value = value[: int(physical["length"])]

    # 4. Enforce single-column uniqueness deterministically.
    if field.id in unique_ids:
        key = (table.id, field.id)
        seen = used_unique.setdefault(key, set())
        attempt = 0
        candidate = value
        while candidate in seen and attempt < 50:
            attempt += 1
            r2 = _rng(seed, table.name, field.name, idx, "uniq", attempt)
            candidate = (_uuid(r2) if physical.get("type") == "uuid"
                         else _generate_scalar(gen, params, r2, idx * 50 + attempt))
            if isinstance(candidate, str) and physical.get("length"):
                candidate = candidate[: int(physical["length"])]
        seen.add(candidate)
        value = candidate
    return value


# --------------------------------------------------------------------------------------------------
# Status plan + small structural helpers.
# --------------------------------------------------------------------------------------------------
def _status_plan(table: Table, schema: SchemaJson, status_dist: dict | None, count: int,
                 seed: int, sm_by_field: dict[str, Any], warnings: list[str]) -> list[Any] | None:
    if not status_dist:
        return None
    field = next((f for f in table.fields if f.name == status_dist.get("field")), None)
    if field is None:
        return None
    allowed = _allowed_states(field, schema, sm_by_field) or []
    plan: list[Any] = []
    for value, n in (status_dist.get("distribution") or {}).items():
        if value in allowed or not allowed:
            plan.extend([value] * int(n))
        else:
            warnings.append(f"{table.name}.{field.name}: status '{value}' is not a reachable state; skipped")
    plan = plan[:count]
    while len(plan) < count:
        r = _rng(seed, table.name, "statusfill", len(plan))
        plan.append(r.choice(allowed) if allowed else None)
    return plan


def _single_pk_name(table: Table) -> str | None:
    pks = table.primary_keys()
    return pks[0].name if pks else None


def _collect_pks(table: Table, rows: list[dict[str, Any]]) -> list[Any]:
    name = _single_pk_name(table)
    if name is None:
        return []
    return [r[name] for r in rows if r.get(name) is not None]


def _fk_field_to(table: Table, parent: Table, schema: SchemaJson) -> str | None:
    for rel in schema.logical.relations:
        if rel.from_table_id == table.id and rel.to_table_id == parent.id and rel.foreign_key_field_id:
            return rel.foreign_key_field_id
    return None


# --------------------------------------------------------------------------------------------------
# SQL rendering.
# --------------------------------------------------------------------------------------------------
def _q(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "'[" + ", ".join(str(v) for v in value) + "]'"  # pgvector / array literal
    if isinstance(value, dict):
        import json
        return "'" + json.dumps(value).replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _render_sql(order: list[Table], generated: dict[str, list[dict]], driver: str) -> list[str]:
    statements: list[str] = []
    for table in order:
        rows = generated.get(table.id, [])
        if not rows:
            continue
        columns = [f.name for f in table.fields]
        col_sql = ", ".join(_q(c) for c in columns)
        for row in rows:
            values = ", ".join(_sql_literal(row.get(c)) for c in columns)
            statements.append(f"INSERT INTO {_q(table.name)} ({col_sql}) VALUES ({values});")
    return statements


# --------------------------------------------------------------------------------------------------
# Optional LLM enrichment (text realism only — never structure; AD-5).
# --------------------------------------------------------------------------------------------------
async def enrich_text(result: dict[str, Any], llm: Any) -> dict[str, Any]:
    """Best-effort: ask an LLM to make free-text values more realistic. No-op without an LLM, and any
    failure leaves the deterministic data untouched (the seeder never depends on a model)."""
    return result  # placeholder hook — structure/relations are always deterministic; text-only, opt-in
