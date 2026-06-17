# Visual Database Designer (Phase 6F)

The **fourth expert module** of the AI Software Architect Platform — the database-intelligence layer.
It turns a plain-language description into a normalized database schema, validates it, and exports
it to SQL, a Laravel migration, a Prisma schema, a Mermaid ERD and an OpenAPI stub. It also ships an
interactive drag & drop canvas.

```
User: "I need a clothing store database"
   ↓
[Visual Database Designer]
   ├─ AI/heuristic design → users, products, orders, …
   ├─ Validation          → ✅ valid · ⚠️ 3 warnings
   └─ Export              → SQL · Laravel · Prisma · Mermaid · OpenAPI
```

## What it is

| Property   | Value |
|------------|-------|
| Name       | `visual_database_designer` |
| Protocol   | Module Protocol v1 (standalone HTTP service) |
| Type       | `generation` (a schema *generator*; "design" module conceptually) |
| Consumes   | — (driven by `feature_request`, threaded via `ctx.settings`) |
| Produces   | `database_schema` |
| Modes      | greenfield + brownfield |
| Needs      | `llm: true` (optional — degrades to deterministic templates) |
| Port       | 9107 |

## How it works

Three layers, mirroring the other expert modules:

```
Interactive canvas (React Flow, /canvas)   ── optional, browser ──┐
                                                                  ▼
HTTP API (SDK build_module_app + extra routes)  /manifest /health /run · /design /validate /export /import
                                                                  ▼
Schema engine  (schema_model · validators · exporters · parsers · comparators · suggestions)
```

## `app/core/` — the production-grade deterministic Core

A second, pure (UI-independent — AD-4) engine built to the specs under `docs/` (`README.md`,
`spec-*.md`, `00-architecture-decisions.md`). It speaks the layered, versioned **`schema_json`**
format with **Stable IDs** and is the source of truth for the diff/risk/migration pipeline. It has
no FastAPI/UI dependency, so the same logic powers the API, a future CLI/headless mode, the embedded
component and the AI-SaaS pipeline. Everything is deterministic; an LLM only ever *suggests* (AD-5).

| Subsystem | Module | Spec | Conformance |
|-----------|--------|------|-------------|
| Stable IDs (AD-1) | `core/ids.py` | spec-schema-json-format §8 | `test_schema_json.py` |
| Layered `schema_json` (AD-3) | `core/schema_json.py` + `schema_json.schema.json` | spec-schema-json-format | `test_schema_json.py` (30) |
| Type System (AD-2) | `core/type_system.py` | spec-type-system | `test_type_system.py` (27) |
| Validation Engine | `core/validation.py` | README §5 + spec §10 | `test_validation.py` (17) |
| Diff Engine | `core/diff.py` | spec-diff-engine | `test_diff.py` (15) |
| Migration Risk Analyzer | `core/risk.py` | spec-migration-risk-analyzer | `test_risk.py` (24) |
| State Machine Designer | `core/state_machine.py` | spec-state-machine-designer | `test_state_machine.py` (15) |

Each subsystem ships with a conformance kit; the build order and the "don't advance until 10/10"
gate from `docs/README.md` §4/§6 were followed. Exposed over HTTP as thin wrappers:

```
POST /core/validate        schema_json → structural errors + validation report (+ SARIF)
POST /core/migrate         any-version schema_json → upgraded to the current formatVersion
POST /core/diff            {from, to[, base]} → typed operation list (+ three-way conflicts)
POST /core/risk            {from, to | operations} → risk report + safe plans + SARIF + exit code
POST /core/state-machine   {stateMachine[, schema]} → enum/rules/tests/admin/api/seed/mermaid
GET  /core/types           the Type Registry (for UI dropdowns)
```

## `/design/*` — Milestone 1: the greenfield pipeline + approval gate

A thin orchestration layer (`app/core/design_session.py`, `suggest.py`, `sql_emitter.py`) on top of
the Core that proves the end-to-end contract: **PRD → AI suggestion → human apply → validate →
submit → approve → migration (executable SQL) → handoff**. The session is itself a small state
machine and *is* the approval gate (AD-5: AI suggests, a human approves):

```
draft ──validate──▶ validated ──submit──▶ pending_approval ──approve──▶ approved
  ▲                     │                        │
  └──── edit ───────────┘                        └── reject ──▶ draft
```

- **Gate rules** (enforced + negatively tested): `/migration` and `/handoff` 409 unless `approved`;
  `approved` is unreachable past a validation error (re-checked at approve); a **critical** op
  (e.g. `drop_table`) blocks approval unless `acknowledgeCritical: true`; `approved` is immutable
  (edit → `revise` starts a new version whose baseline is the approved schema).
- **SQL Emitter** (`core/sql_emitter.py`, Postgres-only for M1): turns the diff operation list +
  target schema into ordered `up`/`down` DDL; physical types come from the Type System, driver
  clauses (`CREATE INDEX CONCURRENTLY`, …) reuse `risk.py`'s data; destructive ops are flagged
  irreversible + `requiresBackup`.
- **AI boundary** (`core/suggest.py`): the LLM only runs in `suggest`; its output passes through a
  deterministic pipeline (assign Stable IDs, resolve semantic types, ensure a PK, drop hallucinated
  relations, structural-validate). The whole path **works with no LLM** via a domain-aware heuristic.
- **Determinism**: `validate`/`diff`/`risk`/`sql` are byte-identical for the same draft.

```
POST /design/sessions                         {mode, prd?, schema_json?} → new session (draft)
POST /design/sessions/{id}/suggest            → {suggestion, diffFromCurrent, rationale} (NOT applied)
POST /design/sessions/{id}/apply-suggestion   {schema_json} → draft updated
POST /design/sessions/{id}/validate           → {state, report:{sarif, summary}}
POST /design/sessions/{id}/submit             → pending_approval (409 if not validated)
POST /design/sessions/{id}/approve            {approvedBy, acknowledgeCritical?} → approved | 409 gate_blocked
POST /design/sessions/{id}/reject / revise    → draft | new revision session
GET  /design/sessions/{id}[/migration|/handoff]   migration & handoff require state=approved
GET  /capabilities                            capability manifest (modes, drivers, endpoints)
```

The M1 acceptance gate lives in `tests/milestones/test_m1_greenfield.py` (marked `conformance`):
positive path + 4 negative gate tests + determinism + an SQL snapshot, plus an opt-in live-Postgres
execution test (`VDB_TEST_POSTGRES_DSN`). `tests/conftest.py` fails loudly on the wrong interpreter.

## `/design/import`, `/design/drift` — Milestone 2: brownfield import + three-way drift

The mirror image of M1: read a real database back into the design world, then keep three sources of
truth honest.

- **Importer** (`app/core/importer.py`) — split into impure `introspect_postgres(dsn)`
  (`information_schema`/`pg_catalog` → plain `IntrospectedSchema`) and **pure, deterministic**
  `build_schema_json(...)`. The build assigns Stable IDs from names (so two imports are
  byte-identical), reverse-infers semantic types (deterministic first, LLM only enriches the
  ambiguous ones via `enrich_ambiguous`, AD-5), rebuilds relations from real FKs (the FK column's
  type is read straight from the DB, so it's structurally correct — the inverse of the bug M1
  caught), detects pivot tables (→ suggest `many_to_many`), preserves real physical types via
  overrides, and reports schema-quality issues as **warnings, not crashes**.
- **Three-way drift** (`app/core/drift.py`, pure) — compares **Designed** (A, Stable IDs) ↔
  **Migrations** (B, a shadow DB the migrations were applied to, then imported) ↔ **Live** (C,
  introspected). `reconcile` matches legs by name (+ structural similarity with a confidence;
  ambiguous matches are *flagged*, never guessed) and adopts A's Stable IDs as canonical. Every
  divergence is classified — `migration_not_applied`, `manual_prod_change`, `design_ahead_of_code`,
  `code_ahead_of_design`, `migration_incomplete`, `synced` — and carries a **suggested** (never
  auto-applied) reconciliation. Output projects to SARIF + an exit code (critical drift fails CI).
- **Shared FK resolution** (`type_system.resolve_fk_physical`, spec §7): M1's emitter fix was
  promoted to the Type System pipeline, so emitter (forward), importer (reverse) and drift all agree
  a designed `foreign_key` column resolves to its referenced PK's type (no spurious drift).
- **Baseline is parametrised** (spec §0): a brownfield session uses the imported schema as *both* the
  initial draft and the migration baseline (`baselineSource: import`), so editing + migration is a
  real delta from the live database — the exact mirror of greenfield's empty baseline.

```
POST /design/import         {dsn, name?, enrich?} → {schema_json, inference:{confident, ambiguous, suggestions}, validation}
POST /design/sessions       {mode: "brownfield", importDsn | schema_json} → session (baselineSource=import)
POST /design/drift          {designed, live|liveDsn, migrations|migrationsDsn|(migrationsDir+shadowDsn), sarif?}
                            → {reconcile:{matched, ambiguous}, drift:[…], summary, exitCode, sarif?}
```

The M2 gate is `tests/milestones/test_m2_brownfield.py` (`conformance`): import snapshot + determinism,
the **round-trip** `emit → apply → import → compare` (locks M1↔M2), all drift categories in one report,
reconcile (no shared id + ambiguous), and opt-in live-Postgres tests (real import, live round-trip, and
three-way drift over real schemas) that **must pass once** on a server to count as proven.

- **`SchemaDesigner`** (`app/designer.py`) ties the stages together: design → validate → export →
  suggest improvements.
- **AI is optional.** With an LLM it asks the model to design the schema and suggest improvements
  (`app/suggestions.py` + `app/prompts.py`); with no LLM it falls back to **domain-aware templates**
  (`app/templates.py` — ecommerce / blog / saas / generic) so the module is fully functional offline
  and tests are deterministic.
- **Validation** (`app/validators.py`) is deterministic and LLM-free: primary keys, FK targets,
  duplicate names, missing indexes, naming conventions → `{valid, errors, warnings}`.
- **Exporters** (`app/exporters.py`): `SQLExporter`, `LaravelMigrationExporter`, `PrismaExporter`,
  `MermaidExporter`, `OpenAPIExporter`.
- **Importers** (`app/parsers.py`): `SQLParser` (CREATE TABLE dump) and `LaravelMigrationParser`
  (`Schema::create` code) for brownfield "design from an existing database".
- **`SchemaComparator`** (`app/comparators.py`) diffs two versions (added/removed/changed
  tables + fields) for version history.

## Endpoints

Pipeline contract (used by the orchestrator):

| Method | Path        | Purpose |
|--------|-------------|---------|
| GET    | `/manifest` | module self-description |
| GET    | `/health`   | liveness |
| POST   | `/run`      | `feature_request` → `database_schema` |

Interactive endpoints (used by the canvas / direct callers):

| Method | Path              | Purpose |
|--------|-------------------|---------|
| POST   | `/design`         | `{feature_request}` → `{database_schema}` (validated + exported) |
| POST   | `/validate`       | `{schema}` → `{validation}` |
| POST   | `/export`         | `{schema, type}` → `{content}` (`type`: sql\|migration\|prisma\|mermaid\|openapi · **django\|sqlalchemy\|typeorm\|sequelize**) |
| POST   | `/import`         | `{type, data}` → `{database_schema}` (`type`: sql\|migration) |
| POST   | `/generate/model` | `{schema, framework, table}` → `{content}` — ORM model/entity class |
| POST   | `/generate/crud`  | `{schema, framework, table, methods}` → `{content}` — CRUD controller |
| GET    | `/frameworks`     | supported `{export, model, crud, crud_methods}` lists (UI dropdowns) |
| GET    | `/field-presets`  | common-column suggestions `{presets:[{label, category, field}]}` |
| POST   | `/compare`        | `{old, new}` → `{diff, migration}` — schema-version diff + SQL migration |
| GET    | `/canvas`         | the drag & drop designer page (static frontend) |

`/export` `type` also accepts **`markdown`** (a data-dictionary doc).

### Enhancements (Phase 6F enhancement roadmap — Phase 1)

The canvas and generators were extended (`Docs/PHASE-6F-ENHANCEMENTS.md`, the HIGH-priority set):

- **Editable canvas** (`frontend/`) — rename / delete / duplicate tables; click any column to edit
  its name, type, length, constraints (PK / auto-increment / nullable / unique / indexed), default and
  enum values; add / delete columns; click a relationship edge to set its **type** (1:1 / 1:∞ / ∞:1 /
  ∞:∞ / polymorphic) and `on_delete` / `on_update`. Tabs: **Design** / **ERD** (Mermaid render) /
  **Code**.
- **Multi-framework code generation** (`app/generators.py`):
  - **Schema export** (#4) adds Django, SQLAlchemy, TypeORM and Sequelize on top of the canonical
    SQL / Laravel / Prisma / Mermaid / OpenAPI.
  - **Model generation** (#21) — `generate_model(schema, framework, table)` for Laravel (Eloquent),
    TypeORM, SQLAlchemy, Django and Prisma, with relationships, casts, `$hidden`, timestamps and
    soft-deletes derived from the schema.
  - **CRUD controllers** (#22) — `generate_crud(schema, framework, table, methods)` for Laravel,
    Express (TypeScript) and Django REST, with validation, error handling, pagination, password
    hashing and proper HTTP status codes.

  All generators are deterministic and LLM-free, and a model + its CRUD controller stay consistent
  because they key off the same `DatabaseSchema`.

**Phase 2 (Quality of Life)** — canvas UX plus two small backend additions:

- **Table groups / domains** (#6) — `Table.group` (optional metadata, round-trips, ignored by
  exporters); the canvas assigns a group per table and colour-codes node headers + the minimap, with a
  legend bar.
- **Search / filter** (#7) — a toolbar box dims non-matching tables (and their edges) live.
- **Undo / redo** (#10) — a 50-deep history stack, `Ctrl+Z` / `Ctrl+Y` (and toolbar buttons); snapshots
  on structural edits (not on drag), skipped while typing in an input.
- **Zoom controls** (#11) — a ReactFlow `Panel` with +/− / Fit / 100%, on top of the built-in Controls
  and MiniMap; `minZoom`/`maxZoom` bounded.
- **Field-type suggestions** (#12) — `app/presets.py` `field_presets()` (a catalog of common columns:
  id, uuid, email, password, slug, is_active, status enum, price, user_id FK, timestamps, …), served at
  `GET /field-presets` and offered as an "Apply preset" picker in the column editor.

**Phase 3 (Advanced)** — schema versioning, enums, composite keys, index management and docs:

- **Schema versioning** (#9) — `app/versioning.py` `compare_schemas` (reuses `SchemaComparator`) +
  `diff_to_migration` (ordered SQL `CREATE` / `ALTER ADD/DROP COLUMN` / `DROP`), served at `POST
  /compare`. The canvas **Versions** tab saves client-side snapshots and compares any two (or the
  current working schema) into a migration script.
- **Reusable enums** (#13) — schema-level `DatabaseSchema.enums` + `SchemaField.enum_ref`;
  `materialize_enums()` resolves a referenced enum's values into the field before validation/export so
  every exporter stays enum-agnostic. The column editor references or creates named enums.
- **Composite keys** (#14) — multiple `primary_key` fields export as `PRIMARY KEY (...)` (SQL),
  `$table->primary([...])` (Laravel) and `@@id([...])` (Prisma); the table-settings panel shows the
  current (possibly composite) key.
- **Index management** (#15) — `Table.indexes` (`Index{name, columns, unique, type}`, `type`:
  btree|fulltext) export to `CREATE [UNIQUE] INDEX` / GIN fulltext (SQL), `$table->index/unique/fullText`
  (Laravel) and `@@index/@@unique` (Prisma); managed in the table-settings panel.
- **Comments / documentation** (#16) — `Table.description` + `SchemaField.description` export as
  `COMMENT ON …` (SQL) and `->comment()` (Laravel); a new **Markdown data-dictionary** exporter
  (`/export type=markdown`) lists tables, columns, constraints, indexes, relations and enums.

The full 22-feature enhancement roadmap (`Docs/PHASE-6F-ENHANCEMENTS.md`) is now implemented across
Phases 1–3.

### `/run` input

```json
{
  "request_id": "uuid",
  "project_id": "uuid",
  "mode": "greenfield",
  "inputs": { "feature_request": "Build a clothing store", "existing_database": null },
  "context": { "llm": { "gateway_url": "...", "token": "..." } },
  "settings": { "database_type": "sql", "driver": "postgresql", "ai_suggestions": true }
}
```

`feature_request` may arrive as an explicit input **or** via `ctx.settings["feature_request"]` (the
platform threads brownfield project settings through — the same path the Feature Implementation
module uses). `settings` keys: `database_type` (sql|nosql|vector), `driver`
(postgresql|mysql|mongodb|sqlite), `ai_suggestions` (default true), `import_type` (for
`existing_database`).

### `/run` output (`database_schema`)

```json
{
  "id": "template-ecommerce", "version": 1, "type": "sql", "driver": "postgresql",
  "tables": [ { "name": "users", "fields": [ ... ], "relations": [ ... ] } ],
  "relations": [ ... ],
  "validation": { "valid": true, "errors": [], "warnings": [ ... ] },
  "exports": { "sql": "CREATE TABLE ...", "migration": "...", "prisma": "...", "mermaid": "...", "openapi": "..." },
  "suggestions": [ "Add an index on ..." ]
}
```

## Develop

Run from this directory with the SDK virtualenv (system Python lacks the deps):

```bash
# tests
../../../packages/module-sdk-python/.venv/Scripts/python.exe -m pytest -q
# lint
../../../packages/module-sdk-python/.venv/Scripts/python.exe -m ruff check .
# run it (then open http://127.0.0.1:9107/canvas)
../../../packages/module-sdk-python/.venv/Scripts/python.exe -m uvicorn app.main:app --port 9107
# conformance
../../../packages/module-sdk-python/.venv/Scripts/conformance-test.exe http://127.0.0.1:9107 --input tests/conformance_sample.json
```

67 module tests + ruff clean + conformance green.

## Build & deploy

The Docker build context **must be the repo root** (it needs `packages/module-sdk-python`):

```bash
docker build -f services/modules/visual-database-designer/Dockerfile -t visual-database-designer .
```

Wired into `docker-compose.yml` (service `visual-database-designer`, port 9107),
`infrastructure/modules.yml` (registry), and `infrastructure/operations.yml` (the `design_database`
operation, target `database_schema`). The Panel surfaces it on the **Analyze** page (operation
"Design database") and renders the result in a **Database Design** tab.

## User guide

1. **Describe** — say what you need (e.g. "a clothing store with products, orders and payments").
2. **AI suggests** — tables and relationships are designed for you (offline fallback uses a
   domain-aware template).
3. **Visualize** — drag & drop the tables on the canvas, add/edit fields, draw relationships.
4. **Validate** — check for errors and advisory warnings.
5. **Export** — copy SQL, a Laravel migration, a Prisma schema, a Mermaid ERD or an OpenAPI stub.
6. **Import** — bootstrap a design from an existing SQL dump or Laravel migration (brownfield).
