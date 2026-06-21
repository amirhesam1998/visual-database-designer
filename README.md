# Visual Database Designer

> The fourth expert module of the AI Software Architect Platform (Phase 6F). It turns a plain-language
> feature description — or an existing database — into a **normalized, validated database schema** with
> ready-to-use exports (SQL, Laravel migration, Prisma, Mermaid ERD, OpenAPI), an interactive drag &
> drop canvas, an approval-gated migration pipeline, and brownfield import + drift detection.

---

## Table of contents

1. [What this module is](#1-what-this-module-is)
2. [The problem it solves](#2-the-problem-it-solves)
3. [Features](#3-features)
4. [Architecture & how it works internally](#4-architecture--how-it-works-internally)
5. [Folder / file structure](#5-folder--file-structure)
6. [Main classes, services, models, routes, views](#6-main-classes-services-models-routes-views)
7. [Data model: "tables" & relationships](#7-data-model-tables--relationships)
8. [API endpoints](#8-api-endpoints)
9. [Admin panel & frontend behavior](#9-admin-panel--frontend-behavior)
10. [Business rules](#10-business-rules)
11. [Validation rules](#11-validation-rules)
12. [Data flow](#12-data-flow)
13. [Usage examples](#13-usage-examples)
14. [Configuration](#14-configuration)
15. [Dependencies](#15-dependencies)
16. [Known limitations](#16-known-limitations)
17. [Future improvements](#17-future-improvements)
18. [Testing](#18-testing)

---

## 1. What this module is

The Visual Database Designer is a **standalone microservice** (a "plugin module" in the platform's
Module Protocol v1). It runs as its own FastAPI app on **port 9107** and can be driven three ways:

- **As a platform pipeline stage** — the AI Orchestrator calls `POST /run` with a `feature_request`
  and gets back a `database_schema` artifact (the Module Protocol contract).
- **As an interactive tool** — a built-in React Flow **designer** (served at `/designer`) calls the
  engine endpoints (`/design/render`, `/core/validate`, `/core/diff`, `/design/code`, …) to design,
  validate, diff/approve and generate code from schemas visually. (The legacy no-build `/canvas` SPA
  has been removed; `/designer` is now the sole visual reference.)
- **As a production design engine** — the `/core/*` and `/design/*` endpoints expose a deterministic,
  approval-gated pipeline that takes a schema from *draft → approved → executable migration → handoff*,
  plus brownfield **import** from a live Postgres database and three-way **drift** detection.

It is **AI-optional**: with an LLM configured it asks the model to design schemas and propose
improvements; with no LLM it falls back to deterministic, domain-aware templates and heuristics, so it
is fully functional offline and its tests are reproducible.

> **Conceptually it's a "design" module, but the Module Protocol `type` is `generation`** (it produces
> a `database_schema`). It declares `consumes=[]`, `produces="database_schema"`, `modes=[greenfield,
> brownfield]`, `needs.llm=True` (optional at runtime).

---

## 2. The problem it solves

Designing a correct relational schema is slow and error-prone, and the design usually rots the moment
code ships:

- **Greenfield:** going from an idea to a normalized schema + migrations by hand is tedious; teams
  forget indexes on foreign keys, store money as floats, leave PKs off tables, expose passwords, etc.
- **Brownfield:** an existing database has no machine-readable "design"; you can't safely evolve what
  you can't see, and reverse-engineering it by hand is unreliable.
- **Drift:** the *designed* schema, the *migration files*, and the *live production database* silently
  diverge. Someone hot-fixes prod with a manual `ALTER`; a migration is written but never deployed; the
  design races ahead of the code. Nobody has a single view of all three.
- **Safety:** auto-applying schema changes is dangerous (a `DROP TABLE` destroys data). Changes need a
  human approval gate and a risk assessment before any DDL runs.

This module addresses all four: it **designs** schemas (with quality validation), **exports** them to
many targets, **imports** real databases back into an editable design, **detects drift** across the
three sources of truth, and gates every migration behind an **explicit human approval** with a risk
analysis — never auto-applying anything.

---

## 3. Features

**Design & generation**
- Natural-language → schema (LLM) with a deterministic domain-aware template fallback
  (ecommerce / blog / saas / generic).
- Deterministic schema **validation** (`{valid, errors, warnings}`).
- **Exports:** SQL DDL, Laravel 11 migration, Prisma schema, Mermaid ERD, OpenAPI 3.0 stub, Markdown
  data dictionary.
- **Framework schema exports:** Django, SQLAlchemy, TypeORM, Sequelize.
- **Model generators** (one table → ORM class): Laravel Eloquent, TypeORM, SQLAlchemy, Django, Prisma.
- **CRUD generators** (one table → controller): Laravel, Express (TypeScript), Django REST.
- **Schema versioning:** diff two versions → ordered SQL migration.

**Visual designer** (`/designer`) — engine-connected, the sole UI reference
- Edit tables, columns and relationships; every edit round-trips through `/design/render` +
  `/core/validate` (the front-end decides nothing — it shows what the engine returns).
- Generate a schema from a description (greenfield session + `suggest` + `apply`).
- **Import / connect an existing database** (`/design/import`): a live Postgres connection **or** a
  SQL/DDL file (applied to a server-side shadow DB, then introspected — no SQL parsed in the browser).
  Ambiguous reverse-inferences are surfaced for the human to confirm (AD-5); the imported map becomes
  a brownfield baseline you can edit, then diff/approve.
- **Compare with database** (`/design/drift`): three-way drift (design ↔ migrations ↔ live), shown as
  a readable report **and** reflected on the canvas (design-only = green, type drift = yellow).
- Duplicate tables (fresh Stable IDs), reusable named enums, explicit/composite indexes, table
  description/domain, and a timestamps/soft-delete toggle that expands to **real** datetime columns.
- Diff vs the approved base + the engine approval gate (`/core/diff` + `/core/risk` + sessions).
- **Code** panel: SQL DDL & OpenAPI (engine-native) plus ORM models, CRUD controllers and framework
  schema exports through the server-side code-gen **bridge** (`/design/code`).
- Search/filter, undo/redo (50-deep), zoom in/out/fit/100%, light & dark themes.

> The Mermaid ERD tab was intentionally **not** migrated — the canvas itself is a live ERD.

**Production Core (`app/core/`) — deterministic schema engine**
- **Stable IDs** (AD-1), **two-layer Type System** (semantic ↔ physical, AD-2), **layered/versioned
  `schema_json`** with a bundled JSON Schema (AD-3), a **Validation Engine** (referential + quality +
  security + performance, SARIF output), a **Diff Engine** (id-based, ordered operation list,
  three-way), a **Migration Risk Analyzer** (expand/contract, rolling-vs-downtime, exit codes), a
  **State Machine Designer**, and a **SQL Emitter** (Postgres up/down DDL).

**Greenfield pipeline + approval gate (Milestone 1, `/design/*`)**
- A design-session state machine (`draft → validated → pending_approval → approved`) that **is** the
  approval gate (AD-5: AI suggests, a human approves). Migration & handoff are reachable only once
  approved; a critical risk blocks approval unless explicitly acknowledged.

**Brownfield import + three-way drift (Milestone 2, `/design/import`, `/design/drift`)**
- **Importer:** introspect a live Postgres database → deterministic, Stable-ID `schema_json` (reverse
  type inference, relations rebuilt from real FKs, pivot detection, enums, checks).
- **Three-way drift:** compare **Designed ↔ Migrations ↔ Live**, classify every divergence, suggest a
  reconciliation (never auto-fix), and emit SARIF + a CI exit code.

**Scenario-based seeder (Milestone 3, `/design/seed`)**
- Fills a schema with **valid, insertable data**: deterministic for a numeric `seed`, FK values are
  *real* primary keys of generated referenced rows (via `resolve_fk_physical`), unique/nullable/enum
  respected, status columns get only **reachable** state-machine states, and declarative *scenarios*
  (counts + status distributions + "derive" rules) produce consistent dependent rows (a delivered order
  gets a successful payment). Output is SQL `INSERT`s (topological order) or JSON; applying it is an
  explicit step (no auto-apply). The LLM, if any, only enriches free text.

**API Contract — OpenAPI as source of truth (Milestone 4, `/design/api/*`)**
- **OpenAPI 3.1** is generated **deterministically** from `schema_json` and is the single source of
  truth: the reference server and the client both derive from it (and the compact contract it shares),
  never re-derived from the schema, so all generators agree. Each table → CRUD paths under `/{version}`,
  an output model (`Order`) and an input model (`OrderCreate`, read-only PK/timestamps excluded), with
  field types from the **resolved Type System** — a foreign-key field inherits the referenced PK's type
  (so `orders.user_id` is a `string/uuid`, never an integer). Status columns expose their reachable
  state-machine states as an `enum`; errors are documented with an RFC 7807 `Problem` schema.
- **Reference FastAPI server** (`/design/api/server`, proof vehicle, not a product): a self-contained,
  standalone `main.py` driven by the embedded contract; validates every body against the contract
  **before** the database (RFC 7807 `problem+json`, per-field `errors` → 422), maps unique/FK violations
  → 409, missing → 404, and enforces state-machine transitions on `PATCH` (illegal → 422). One target
  (FastAPI/Python), one driver (Postgres); CRUD + light nested reads. No auto-deploy.
- **Client** (`/design/api/client`, secondary): a `fetch`-based TypeScript client + Postman collection,
  derived from the OpenAPI document. The LLM, if any, only enriches descriptions/examples.

**End-to-end integration (`/design/run-e2e`)**
- A thin orchestrator that runs the whole chain back-to-back on one real Postgres and reports each step:
  greenfield `suggest → [approval] → migration → seed → API → real GET/POST`, or brownfield
  `import → drift → seed → API`. It adds **no new logic** — every step's output is the next step's input,
  untouched (the approved `handoff["schema_json"]` flows through migration, seed and the API server). The
  only pause is the human approval gate (`autoApprove` is for automated runs); without approval nothing
  downstream runs. Each step reports `ok`; a failure stops the chain and names the exact step (the point
  is to surface any contract mismatch, not to paper over it). It actually applies the migration, inserts
  the seed and drives the generated M4 server over HTTP — a runnable demo of the entire product.
  *(Building this surfaced and fixed one real leak: the AI-suggest step produced relations without a
  `foreignKeyFieldId`, so the emitter guessed a `<table>_id` column and the FK type wasn't resolved;
  `suggest._normalize` now links every relation to its foreign-key field.)*

---

## 4. Architecture & how it works internally

This module contains **two parallel layers** that a newcomer must not confuse. They use **two different
schema representations**:

| | "Simple" designer layer (`app/*.py`) | Production Core (`app/core/*.py`) |
|---|---|---|
| **Model** | `DatabaseSchema` (Pydantic) | layered `schema_json` (dict + JSON Schema) |
| **Keys** | snake_case, `FieldType` enum (`varchar`, `bigint`, …) | camelCase, **semantic** types (`email`, `money`, `uuid`, …) |
| **Identity** | by **name** | **Stable IDs** (`tbl_…`, `fld_…`, `rel_…`) — renames are first-class |
| **Layers** | flat (tables/fields/relations) | `logical` / `physical` / `semantic` / `presentation` |
| **Used by** | `/run` pipeline, the canvas, `/design`, `/validate`, `/export`, `/import`, `/generate`, `/compare` | `/core/*`, `/design/*` sessions, `/design/import`, `/design/drift` |
| **Purpose** | fast, friendly, multi-target generation | rigorous, deterministic, production migration/drift |

Both layers ship in the same service. The simple layer powers the everyday "describe → design →
export" flow and the visual canvas. The Core powers the milestone pipelines (greenfield approval gate,
brownfield import/drift) where correctness, determinism and safety matter most.

### Five architecture decisions (the Core's backbone)

The Core is built on five decisions documented in `docs/00-architecture-decisions.md`:

- **AD-1 — Stable IDs.** Every entity has an immutable `id`; all references use the id, so a rename is
  a first-class operation (not drop+add) and diff/merge are semantic.
- **AD-2 — Two-layer Type System.** A field stores only a `semanticType` (+ optional overrides); one
  registry record deterministically resolves the physical type, validation rules, form widget, OpenAPI,
  fake-data generator and privacy class for every consumer — no knowledge is re-derived elsewhere.
- **AD-3 — Layered, versioned `schema_json`.** The document has `logical` / `physical` / `semantic` /
  `presentation` layers; the `presentation` layer (canvas positions) is **never** schema-affecting.
  Format migrations are deterministic and registered by major version.
- **AD-4 — Core-first.** All real logic lives in pure Core functions; the routes and UI are thin
  wrappers. Everything in `app/core/` is deterministic and (mostly) LLM-free.
- **AD-5 — AI suggests, a human approves.** The LLM only ever produces *suggestions*; everything after
  the suggestion is deterministic and reproducible, and no migration/handoff is produced without an
  explicit human approval.

### Determinism

`validate` / `diff` / `risk` / `sql` and **import** are byte-identical for the same input. The Diff
Engine uses a composite sort key (not set-iteration order) so the operation list — and the SQL derived
from it — is reproducible across processes. The importer derives every Stable ID from a deterministic
hash of names, so two imports of the same database produce identical `schema_json`.

---

## 5. Folder / file structure

```
visual-database-designer/
├── app/
│   ├── main.py                 # ASGI entry point (`uvicorn app.main:app`)
│   ├── module.py               # VisualDatabaseDesignerModule (Module Protocol: /manifest /health /run)
│   ├── routes.py               # register_interactive_routes(app) — ALL extra HTTP endpoints
│   │
│   │   # ── Simple designer layer ──────────────────────────────────────────
│   ├── schema_model.py         # DatabaseSchema / Table / SchemaField / Relation / Index / EnumDef
│   ├── output.py               # DatabaseSchemaResult (the `/run` output payload)
│   ├── designer.py             # SchemaDesigner — orchestrates design→validate→export→suggest
│   ├── suggestions.py          # SchemaSuggestions — LLM design + improvement advice
│   ├── templates.py            # build_template_schema — offline domain templates
│   ├── prompts.py              # LLM system/user prompt strings
│   ├── validators.py           # SchemaValidator — {valid, errors, warnings}
│   ├── exporters.py            # SQL / Laravel / Prisma / Mermaid / OpenAPI / Markdown exporters
│   ├── generators.py           # framework schema + ORM model + CRUD controller generators
│   ├── parsers.py              # SQLParser, LaravelMigrationParser (regex-based import)
│   ├── comparators.py          # SchemaComparator — version diff
│   ├── versioning.py           # compare_schemas + diff_to_migration
│   ├── presets.py              # field_presets() — common-column catalog for the canvas
│   │
│   └── core/                   # ── Production Core (deterministic schema_json engine) ──
│       ├── schema_json.py          # layered schema_json models + load/migrate/validate_structure
│       ├── schema_json.schema.json # bundled JSON Schema (structural validation)
│       ├── ids.py                  # Stable IDs (AD-1): prefixed ULIDs
│       ├── type_system.py          # Type Registry, ResolvedField, reverse inference, resolve_fk_physical
│       ├── validation.py           # Validation Engine (referential/quality/security/perf, SARIF)
│       ├── diff.py                 # Diff Engine (operation list, three-way conflict detect)
│       ├── risk.py                 # Migration Risk Analyzer (levels, safe plan, SARIF, exit codes)
│       ├── state_machine.py        # State Machine Designer (one definition → 6 outputs)
│       ├── sql_emitter.py          # SQL Emitter (Postgres up/down DDL from the operation list)
│       ├── design_session.py       # DesignSession + SessionStore (M1 state machine + approval gate)
│       ├── suggest.py              # AI suggest (the only LLM touchpoint of the greenfield path)
│       ├── importer.py             # M2 brownfield: introspect_postgres + build_schema_json + apply_sql
│       ├── drift.py                # M2 three-way drift: reconcile + three_way_drift + SARIF
│       └── seeder.py               # M3 scenario seeder: deterministic, FK/enum/state-consistent data
│
├── frontend-canvas/            # The visual designer SPA — Vite+React+TS+Tailwind+React Flow (served at /designer)
│   ├── src/lib/                # types, schema (editable doc + structural mutations), api, graph, layout, diffStyle
│   ├── src/store/              # centralized zustand state: editable doc, engine-resolved view, diff, approve gate
│   ├── src/components/canvas/  # TableNode, Canvas, RelationDialog, ApproveDialog, GenerateDialog, EnumDialog
│   ├── src/components/panels/  # Toolbar (generate/add/enums/undo/zoom/code/changes/approve), DetailsPanel, CodePanel, DiffPanel
│   └── src/**/*.test.ts(x)     # Vitest + Testing Library: schema mutations, engine-validated edits, FK guard, diff/approve gate
│
├── docs/                       # Architecture decisions + per-engine specs + milestone specs
├── tests/
│   ├── conftest.py             # FAILS LOUDLY on the wrong interpreter (must be the SDK venv, py3.12)
│   ├── core/                   # unit tests per Core engine
│   ├── milestones/             # conformance kits: test_m1_greenfield.py, test_m2_brownfield.py
│   └── test_module.py          # Module Protocol smoke tests
├── Dockerfile                  # python:3.12-slim; installs SDK + requirements; runs uvicorn :9107
├── requirements.txt            # uvicorn + jsonschema + psycopg[binary]
└── pyproject.toml              # pytest config + markers (conformance, live_postgres) + ruff
```

---

## 6. Main classes, services, models, routes, views

### Module entry (`app/module.py`)
- **`VisualDatabaseDesignerModule(Module)`** — declares the manifest, `output_model =
  DatabaseSchemaResult`, and implements `run()` (resolves `feature_request`/`existing_database` from
  inputs or `ctx.settings`, runs `SchemaDesigner`, returns the schema) and `health()`.
- `app = build_module_app(module)` then `register_interactive_routes(app)` mounts everything else.

### Simple layer "services"
- **`SchemaDesigner`** (`designer.py`) — the orchestration service: `design()` (request → schema →
  validate → export → suggest), `validate()`, `_assemble()`.
- **`SchemaSuggestions`** (`suggestions.py`) — `suggest_schema()` (LLM or template fallback) and
  `suggest_improvements()` (advisory strings).
- **`SchemaValidator`** (`validators.py`) — deterministic `{valid, errors, warnings}`.
- **Exporters** (`exporters.py`) — `SQLExporter`, `LaravelMigrationExporter`, `PrismaExporter`,
  `MermaidExporter`, `OpenAPIExporter`, `MarkdownDocExporter` (façade: `EXPORTERS`, `export_one`,
  `export_all`).
- **Generators** (`generators.py`) — `export_framework_schema`, `generate_model`, `generate_crud`,
  `supported_frameworks`.
- **Parsers** (`parsers.py`) — `SQLParser`, `LaravelMigrationParser`, `parse_import`.
- **`SchemaComparator`** (`comparators.py`) + `versioning.compare_schemas` / `diff_to_migration`.

### Simple layer models (`schema_model.py`, `output.py`)
- **`DatabaseSchema`** → `tables[]`, `enums[]`, `driver`, `type`, `version`, helpers (`table()`,
  `enum()`, `all_relations()`, `materialize_enums()`).
- **`Table`** → `fields[]`, `relations[]`, `indexes[]`, `timestamps`, `soft_delete`, `group`,
  `description`.
- **`SchemaField`** → `name`, `type` (`FieldType`), `nullable`, `unique`, `indexed`, `primary_key`,
  `auto_increment`, `default`, `length/precision/scale`, `values`/`enum_ref`, `description`.
- **`Relation`** → `from_table/from_field`, `to_table/to_field`, `type` (`RelationType`), `on_delete`,
  `on_update`.
- **`Index`** → `name`, `columns`, `unique`, `type` (`btree`|`fulltext`).
- **`DatabaseSchemaResult`** (`output.py`) — the `/run` output: `tables`, `relations`, `validation`,
  `exports{sql,migration,prisma,mermaid,openapi,markdown}`, `suggestions`.

### Core layer (`app/core/`)
- **`schema_json`** — `SchemaJson` (+ `Table`, `Field_`, `Relation`, `Index`, `EnumDef`, `StateMachine`,
  layers); `load()`, `migrate()`, `validate_structure()`, `dump()`.
- **`type_system`** — `TypeRegistry`, `SemanticTypeDef`, `ResolvedField`, `infer_semantic_type()`
  (reverse inference for import), `resolve_fk_physical()` (an FK column inherits the referenced PK's
  physical type — shared by emitter, importer and drift), `DEFAULT_REGISTRY` (33 semantic types).
- **`validation`** — `validate()` → `ValidationReport` (`Finding{rule_id, severity, message, fix}`,
  SARIF).
- **`diff`** — `diff()` → `DiffResult.op_dicts()` (the ordered operation list); `three_way_diff()`.
- **`risk`** — `analyze()` → `RiskReport` (`RiskLevel`, `OperationRisk`, `gate()`, `checklist()`,
  `to_sarif()`).
- **`sql_emitter`** — `emit_sql(operations, schema, driver="postgres")` → `SqlScript`
  (`up_statements()`, `down_statements()`, `requires_backup`).
- **`design_session`** — `DesignSession`, `SessionStore` (the M1 state machine + approval gate);
  exceptions `SessionNotFoundError` (404), `InvalidTransitionError` (409), `GateBlockedError` (409).
- **`suggest`** — `suggest_schema(prd, llm=None)` (greenfield AI suggestion + deterministic normalize).
- **`importer`** — `introspect_postgres(dsn)` (impure), `build_schema_json()` (pure/deterministic),
  `apply_sql()` (shadow-DB applier), `split_sql()`, `import_sql_via_shadow(sql, shadowDsn)` (file
  import: apply a DDL dump to a shadow DB then introspect it), `enrich_ambiguous()`.
- **`drift`** — `reconcile()`, `three_way_drift()` → `DriftReport` (`DriftEntry`, `to_sarif()`,
  `exit_code`).
- **`seeder`** — `seed_data(schema_json, seed, scenario, output)` → `{rows, sql|data, warnings}`;
  `topological_order()`, `resolve_scenario()`, `SeedError` (NOT NULL FK cycle), `enrich_text()`.

### "Controllers" & "views"
There are no MVC controllers — the HTTP layer is FastAPI route functions defined in
`register_interactive_routes()` (`routes.py`). The only "view" is the built **designer** SPA in
`frontend-canvas/` (served at `/designer`).

---

## 7. Data model: "tables" & relationships

**This module does not own any persistent database tables.** It is effectively stateless:

- The platform's shared Postgres/pgvector is **not** used by this module for its own storage.
- **Design sessions** (the M1 pipeline) live in an **in-memory** `SessionStore` (a process-local dict);
  a production deployment would swap this for a persistent store behind the same API. Sessions are lost
  on restart.
- The databases this module reads (brownfield `/design/import`, drift `liveDsn`/shadow DB) are the
  **user's** databases, accessed read-only for introspection (the shadow-DB applier writes only to a
  throwaway shadow DB the caller provides).

The "tables and relationships" this module manages are therefore the **user's designed schema**, in one
of the two representations above. The canonical, production representation is `schema_json`:

```jsonc
{
  "formatVersion": "1.0.0",
  "meta":   { "name": "shop", "databaseType": "postgres", "defaultDriver": "postgres" },
  "logical": {
    "tables": [
      { "id": "tbl_…", "name": "users", "kind": "normal", "fields": [
        { "id": "fld_…", "name": "id",    "semanticType": "uuid",  "isPrimaryKey": true, "nullable": false },
        { "id": "fld_…", "name": "email", "semanticType": "email", "nullable": false }
      ]}
    ],
    "relations": [
      { "id": "rel_…", "type": "one_to_many", "fromTableId": "tbl_orders",
        "toTableId": "tbl_users", "foreignKeyFieldId": "fld_user_id", "onDelete": "cascade" }
    ],
    "enums": [ { "id": "enm_…", "name": "order_status", "values": [ {"value": "pending"} ] } ]
  },
  "physical":     { "indexes": [ { "id": "idx_…", "tableId": "tbl_…", "columns": ["fld_…"], "unique": true } ] },
  "semantic":     { "businessRules": [], "stateMachines": [] },
  "presentation": { "nodes": [ { "tableId": "tbl_…", "x": 60, "y": 80 } ] }
}
```

**Relationship types:** `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`, `polymorphic`
(and in `schema_json` also `self`, `has_many_through`, `embedded`). Cardinality on import is inferred
from FK-column uniqueness; pivot tables (two single-column FKs whose composite PK is exactly those two
columns) are detected and a `many_to_many` is *suggested*.

---

## 8. API endpoints

All endpoints are served by the single FastAPI app on **port 9107**.

### Module Protocol (used by the orchestrator)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/manifest` | module self-description |
| GET  | `/health`   | liveness `{status, module, version}` |
| POST | `/run`      | `feature_request` → `database_schema` (the pipeline contract) |

### Simple designer / canvas endpoints

| Method | Path | Body → Result |
|---|---|---|
| POST | `/design`         | `{feature_request, settings?, existing_database?}` → `{database_schema}` |
| POST | `/validate`       | `{schema}` → `{validation:{valid,errors,warnings}}` |
| POST | `/export`         | `{schema, type}` → `{content}` — `type`: `sql`\|`migration`\|`prisma`\|`mermaid`\|`openapi`\|`markdown`\|`django`\|`sqlalchemy`\|`typeorm`\|`sequelize` |
| POST | `/import`         | `{type, data}` → `{database_schema}` — `type`: `sql`\|`migration` (regex parsers) |
| POST | `/generate/model` | `{schema, framework, table}` → `{content}` — ORM model class |
| POST | `/generate/crud`  | `{schema, framework, table, methods?}` → `{content}` — CRUD controller |
| GET  | `/frameworks`     | supported `{export, model, crud, crud_methods}` lists |
| GET  | `/field-presets`  | common-column catalog for the canvas |
| POST | `/compare`        | `{old, new}` → `{diff, migration}` — version diff + SQL migration |

> These legacy REST endpoints operate on the simpler `DatabaseSchema` model and remain the module's
> stable framework-agnostic API. The visual designer no longer uses them — it drives the `schema_json`
> engine endpoints below (and `/design/code` for generation).

### Designer code-generation (bridge) endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/code` | `{schema_json, kind, framework?, table?, methods?}` → `{content}`. `kind`: `sql` (engine-native) \| `model` \| `crud` \| `schema`. ORM/CRUD/exports reuse the proven generators via the `schema_json → DatabaseSchema` bridge — FK physical types (uuid stays uuid) and semantic types are preserved in translation. |
| GET  | `/design/code/frameworks` | `{sql, model, crud, crudMethods, schema}` lists for the Code panel's dropdowns |

### Core (deterministic `schema_json`) endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/core/migrate`       | upgrade a payload to the current `formatVersion` + structural errors |
| POST | `/core/validate`      | `{structuralErrors, report, sarif?}` |
| POST | `/core/diff`          | operation list (+ `threeWay` if `base` supplied) |
| POST | `/core/risk`          | migration risk report (+ `checklist`, `sarif?`) |
| POST | `/core/state-machine` | derive all outputs from a state-machine definition |
| GET  | `/core/types`         | the Type Registry (id, category, physical, pii) |

### Design-session pipeline (Milestone 1 — greenfield + approval gate)

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/sessions`                       | `{mode, prd?, schema_json?}` → new session (`draft`) |
| POST | `/design/sessions/{id}/suggest`          | AI suggestion `{suggestion, diffFromCurrent, rationale}` (NOT applied) |
| POST | `/design/sessions/{id}/apply-suggestion` | `{schema_json}` → draft updated |
| POST | `/design/sessions/{id}/validate`         | `{state, report:{sarif, summary, structuralErrors}}` |
| POST | `/design/sessions/{id}/submit`           | → `pending_approval` (409 if not validated) |
| POST | `/design/sessions/{id}/approve`          | `{approvedBy, acknowledgeCritical?}` → `approved` \| 409 `gate_blocked` |
| POST | `/design/sessions/{id}/reject`           | → `draft` |
| POST | `/design/sessions/{id}/revise`           | → a new editable revision (baseline = approved schema) |
| GET  | `/design/sessions/{id}`                  | the session view |
| GET  | `/design/sessions/{id}/migration`        | risk-checked up/down DDL — **409 unless approved** |
| GET  | `/design/sessions/{id}/handoff`          | the approved handoff artifact (+ checksum) — **409 unless approved** |

### Brownfield (Milestone 2 — import + drift)

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/import`                  | live: `{dsn, name?, enrich?}` · file: `{sql\|ddl, shadowDsn? (else `VDB_SHADOW_DSN`), name?}` → `{schema_json, inference:{confident,ambiguous,suggestions}, validation}` |
| POST | `/design/sessions` (brownfield)   | `{mode:"brownfield", importDsn \| schema_json}` → session with `baselineSource:"import"` |
| POST | `/design/drift`                   | `{designed, live\|liveDsn, migrations\|migrationsDsn\|(migrationsDir+shadowDsn), sarif?}` → `{reconcile, drift, summary, exitCode, sarif?}` |
| GET  | `/capabilities`                   | capability manifest (modes, drivers, drift categories, scenarios, guarantees) |

### Scenario seeder (Milestone 3)

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/seed` | `{schema_json \| handoff, seed?, scenario?, output: sql\|json, enrich?}` → `{rows, sql\|data, warnings}` — deterministic, FK/enum/state-consistent data (preset scenarios: `ecommerce_medium`, `multi_tenant`, `ticketing`) |

### API contract (Milestone 4)

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/api/contract` | `{schema_json \| handoff, version?}` → `{openapi, stats:{resources,paths,operations}}` — deterministic OpenAPI 3.1, the source of truth |
| POST | `/design/api/server` | `{schema_json \| handoff, target:"fastapi", version?}` → `{files:{main.py, requirements.txt, README.md}, target}` — self-contained reference server (no auto-deploy) |
| POST | `/design/api/client` | `{openapi \| schema_json, target:"typescript"}` → `{files:{client.ts}, postman}` — secondary, derived from the OpenAPI document |

### End-to-end integration

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/run-e2e` | `{mode:"greenfield"\|"brownfield", prd?, dsn, autoApprove?, seed?, scenario?, version?}` → `{steps:[{step, ok, ...}], result:"green"\|"awaiting_approval"\|"red", sessionId, schemaVersion?}` — runs the whole chain on a real Postgres; needs a real `dsn` |

### Canvas — visual designer (Canvas Milestones 1–3: view + edit + diff/approve)

| Method | Path | Purpose |
|---|---|---|
| POST | `/design/render` | `{schema_json \| sessionId \| handoff}` → `{tables, relations, enums, presentation, hasLayout}` — a render projection of a `schema_json`. Reuses the Type System's existing resolution so each field carries its resolved `physicalType` (an FK column inherits the referenced PK's type — a uuid FK is `uuid`, never an int) and a `pii` flag; adds **no** new engine logic. The editing canvas re-renders through this after every change. |
| POST | `/core/validate` | the canvas validates each edit here — findings (with `entity_id`) are shown inline next to the field/table (Canvas M2 §0/§6). |
| GET  | `/core/types` | semantic-type catalogue for the editor's type dropdown (Canvas M2 §1/§3). |
| POST | `/design/presentation` | `{nodes, schema_json? \| sessionId?}` → `{schema_json, persisted}` — saves canvas layout into the `presentation` layer only. Layout is **not** schema-affecting (the diff engine ignores it), so a table move never counts as a schema change and never changes a session's gate state (Canvas M2 §4). The one thin endpoint the spec permits for presentation (§8). |
| POST | `/core/diff` | `{from, to}` → typed operation list + `colored` lines. The canvas shows the change list and tints the canvas with the standard colours; moving a table yields no operations (Canvas M3 §1). |
| POST | `/core/risk` | `{from, to}` → migration risk report. Shown before approve; a `critical` op (e.g. `drop_table`) must be acknowledged (Canvas M3 §3). |
| (gate) | `/design/sessions/*` | Canvas M3 **approve** drives the *existing* approval gate — a brownfield session (baseline = the canvas base) then apply-suggestion → validate → submit → approve `{acknowledgeCritical}`. No new engine logic; the gate decides (spec §0/§6). |
| GET  | `/designer` | the built React Flow + Tailwind **Canvas** SPA (present only when `frontend-canvas/` has been built). |

---

## 9. Admin panel & frontend behavior

**Admin panel:** this module has no admin panel of its own. The platform's Laravel **Panel** service
(port 9000) provides the user-facing UI and proxies to this module; the brownfield/analyze UI and the
result tabs live there. This module simply exposes machine + canvas endpoints.

**Frontend (the canvas):** `frontend/` is a **no-build-step** React Flow single-page app:

- `index.html` loads React 18, ReactDOM, React Flow (v11) and Mermaid from CDNs via an **import map** —
  there is no bundler. `canvas.js` is the entry module; `components/TableNode.js` renders a table node.
- It talks to the module's **own** endpoints (same origin): `POST /design` to generate, `/validate`,
  `/export`, `/generate/model|crud`, `/field-presets`, `/compare`.
- Behaviors: generate from a prompt; edit/rename/delete/duplicate tables; colour-coded groups + minimap
  + legend; click a column to edit type/length/constraints/default/enum and apply field presets; click
  an edge to set relationship type + referential actions; reusable enums; composite keys; explicit
  indexes; table/column comments; search/filter (dims non-matches); undo/redo (50-deep, `Ctrl+Z` /
  `Ctrl+Y`); zoom controls. Tabs: **Design**, **ERD** (live Mermaid render), **Code** (export + generate),
  **Versions** (client-side snapshots compared into a migration script).
- Theme: a single `styles.css`; no theming system or dark mode toggle.

**Canvas (`frontend-canvas/`, served at `/designer/`):** a separate, **build-based**
(Vite + React + TypeScript + Tailwind + React Flow) single-page app — the tool's own UI, now the
**complete** view → edit → diff → approve cycle (Canvas Milestones 1–3). It is an **editing tool, not an engine**:
every change only mutates the local `schema_json` *structurally*, then goes back through the engine —
`POST /design/render` re-resolves types and `POST /core/validate` returns findings shown inline — so
no database logic lives in the front-end (`edit → render + validate → render`, a one-way,
engine-authoritative path). It renders each table as a node (PK/FK icons, a lock on sensitive fields,
semantic zoom, an error mark when the engine flags it), each relation as a directional edge with a
cardinality marker, and auto-lays-out (dagre) schemas without saved `presentation` positions.
Editing: rename/add/delete tables and fields and change a field's **semantic** type from the panel;
draw a relation by dragging between tables (a dialog makes the **FK field mandatory**, so an
incomplete relation is impossible and a uuid PK yields a uuid FK); double-click to rename inline;
reposition tables (saved to `presentation` via `/design/presentation`, **not** counted as a schema
change). **Diff & approve (M3):** a **Changes** panel renders the engine's `/core/diff` operation
list in the standard colours (green=add, red=remove, yellow=change, blue=rename) and tints the
changed tables/fields/relations on the canvas; **Approve** is enabled only once validation is green,
shows the `/core/risk` report, requires explicit acknowledgement for a `critical` op (e.g. dropping a
table — the gate's `acknowledgeCritical`), and on success locks the version and makes it the new base.
Approve drives the *existing* engine gate (no diff/approve/risk logic in the front-end). The toolbar
carries add-table, undo/redo, an "unsaved changes" indicator, a validation summary, **Changes** and
**Approve**. State is centralized (zustand) with light/dark theming (CSS variables, respects the OS
preference, switches with no reload). Tested with **Vitest + Testing Library**
(`cd frontend-canvas && npm install && npm test`); built with `npm run build` into `dist/`.

---

## 10. Business rules

- **AD-5 / Approval gate (the central rule).** No migration or handoff artifact is ever produced from a
  session that is not `approved`. The session is a state machine:
  ```
  draft ──validate──▶ validated ──submit──▶ pending_approval ──approve──▶ approved
    ▲                     │                        │
    └──── edit ───────────┘                        └── reject ──▶ draft
  ```
  - Any edit to a draft drops it back to `draft` and clears the last validation (must re-validate).
  - `submit` requires `validated`; `approve` requires `pending_approval` (else 409).
  - **Approve has three gates:** (1) an approver identity must be present; (2) validation is *re-checked*
    at the moment of approval and must be green; (3) no **critical**-risk migration operation (e.g.
    `drop_table`) may proceed unless the caller passes `acknowledgeCritical: true`.
  - `approved` is **immutable** — editing requires `revise`, which opens a new session whose baseline is
    the approved schema (so a later migration is a true delta).
- **Baseline is parametrised** (the migration delta source): empty for **greenfield**, the approved
  schema for a **revise**, the imported live schema for **brownfield** (`baselineSource: import`, where
  the imported schema is *both* the draft and the baseline).
- **AI boundary.** The LLM only runs in `suggest` (and the optional import `enrich`); its output passes
  through a deterministic normalize pipeline (assign Stable IDs, resolve semantic types, ensure a PK,
  drop hallucinated relations) and is *never* auto-applied — a human applies it.
- **Drift is report-only.** Three-way drift never fixes anything; each divergence carries a *suggested*
  reconciliation. Drift categories and their CI severity:
  | Category | A (designed) | B (migrations) | C (live) | severity | suggestion |
  |---|---|---|---|---|---|
  | `synced`                | ✓ | ✓ | ✓ | none (not reported) | — |
  | `migration_not_applied` | ✓ | ✓ | ✗ | warning | `apply_migration` |
  | `manual_prod_change`    | ✗ | ✗ | ✓ | **error** | `import_to_design` |
  | `design_ahead_of_code`  | ✓ | ✗ | ✗ | note | `generate_migration` |
  | `code_ahead_of_design`  | ✗ | ✓ | ✓ | warning | `import_to_design` |
  | `migration_incomplete`  | ✓ | ✓ | partial | **error** | `apply_migration` |

  `DriftReport.exit_code` is `1` if any **error**-severity drift exists (so CI fails on untracked prod
  changes or half-applied migrations).
- **FK type correctness.** A foreign-key column's physical type is dictated by the referenced primary
  key (`type_system.resolve_fk_physical`); the emitter renders FK columns with the PK's type, the
  importer reads the real type, and drift uses the same resolution so a designed `foreign_key` column
  never shows as spurious drift against a live `uuid` column.
- **Imports never crash on imperfect databases.** A real DB that violates a quality rule (e.g. a
  PK-less table) is reported as a *warning*, not an error.
- **Migration safety.** Two-phase apply (create tables → indexes → FKs); destructive ops are flagged
  irreversible + `requiresBackup`; `CREATE INDEX CONCURRENTLY` is emitted outside a transaction.

---

## 11. Validation rules

There are **two validators**, one per layer.

### Simple-layer `SchemaValidator` (`validators.py`) — `{valid, errors, warnings}`
- **Errors:** no tables; table/field not a valid identifier; table name > 64 chars; duplicate table;
  table with no fields; duplicate field; table with no primary key; relation target table not found.
- **Warnings:** reserved SQL word as a name; composite primary key (confirm intentional); relation
  `from_field`/`to_field` not declared; `*_id` field not indexed; unique field not indexed; non-lowercase
  (snake_case) table/field names.

### Core Validation Engine (`core/validation.py`) — SARIF, stable rule IDs, suppressible
Every finding has a stable `rule_id`, a `severity` (`error`/`warning`/`suggestion`/`security`/
`performance`), a message and often a `fix`. Findings can be suppressed globally or inline via
`vdb-ignore: <rule-id>` in an entity comment. A report is `valid` iff there are **zero error-severity**
findings.

- **Referential (errors):** `REF001`–`REF004` relation table/field references; `REF010`/`REF011` index
  table/columns; `REF020` enum reference; `REF030`–`REF032` ownership/tenancy field references
  (warnings).
- **Quality:** `QLT001` table has no primary key (warning); `QLT002` duplicate field name (error);
  `QLT010` email not covered by a unique index (warning); `QLT011` money stored as float (warning);
  `QLT020` unregistered semantic type (warning); `QLT030` FK-shaped relation with no explicit FK field
  (suggestion).
- **Security:** `SEC001` password field not masked.
- **Performance:** `PRF001` foreign-key field not indexed.
- **State machines:** `SM001`–`SM009` (field binding + structural checks, shared with
  `state_machine.py`).

**Structural validation** is separate: `schema_json.schema.json` (a bundled JSON Schema) enforces shape,
id patterns and required fields, independent of the referential/quality rules above.

---

## 12. Data flow

**Greenfield via the platform pipeline (`/run`):**
```
feature_request ─▶ SchemaDesigner.design()
   ├─ LLM available → SchemaSuggestions.suggest_schema (LLM) ──┐
   └─ no LLM        → build_template_schema (offline) ─────────┤
                                                               ▼
                              DatabaseSchema ─▶ materialize_enums
                                  ├─ SchemaValidator.validate  → {valid,errors,warnings}
                                  ├─ export_all                → {sql,migration,prisma,mermaid,openapi,markdown}
                                  └─ suggest_improvements      → [advice]
                                                               ▼
                                                    DatabaseSchemaResult (database_schema)
```

**Greenfield approval pipeline (`/design/*`, Milestone 1):**
```
POST /design/sessions ─▶ suggest (LLM, optional) ─▶ apply-suggestion ─▶ validate ─▶ submit
   ─▶ approve (3 gates) ─▶ [approved] ─▶ migration (diff → risk → SQL) ─▶ handoff (+checksum)
```

**Brownfield import + drift (Milestone 2):**
```
/design/import:  introspect_postgres(dsn) ─▶ build_schema_json (Stable IDs, reverse-inference,
                 relations from FKs, pivots, enums, checks) ─▶ {schema_json, inference, validation}

/design/drift:   designed (A, authored)         ┐
                 migrations (B = shadow DB,      ├─▶ reconcile (match by name + structural) ─▶
                   migrations applied + imported)│    three_way_drift ─▶ {drift[], summary,
                 live (C = introspect prod)      ┘                        exitCode, sarif}
```

---

## 13. Usage examples

**Run the service locally (from the repo root, using the SDK virtualenv):**
```bash
packages/module-sdk-python/.venv/Scripts/python.exe -m uvicorn app.main:app \
  --app-dir services/modules/visual-database-designer --port 9107
# open http://localhost:9107/designer
```

**Design a schema (pipeline contract):**
```bash
curl -X POST localhost:9107/run -H 'content-type: application/json' -d '{
  "request_id":"r1","project_id":"p1","mode":"greenfield",
  "inputs":{"feature_request":"An online clothing store with products, orders and customers"},
  "settings":{"database_type":"sql","driver":"postgresql","ai_suggestions":true}
}'
```

**Export an existing schema to a Laravel migration:**
```bash
curl -X POST localhost:9107/export -H 'content-type: application/json' \
  -d '{"schema": {...DatabaseSchema...}, "type":"migration"}'
```

**Greenfield approval pipeline (no LLM — supply the schema directly):**
```bash
SID=$(curl -s -X POST localhost:9107/design/sessions -d '{"schema_json": {...}}' | jq -r .sessionId)
curl -X POST localhost:9107/design/sessions/$SID/validate
curl -X POST localhost:9107/design/sessions/$SID/submit
curl -X POST localhost:9107/design/sessions/$SID/approve -d '{"approvedBy":"alice"}'
curl       localhost:9107/design/sessions/$SID/migration   # up/down DDL (only now, post-approval)
```

**Import a live database, then detect drift:**
```bash
curl -X POST localhost:9107/design/import -d '{"dsn":"postgresql://user:pw@host/db"}'
curl -X POST localhost:9107/design/drift -d '{
  "designed": {...schema_json...},
  "liveDsn": "postgresql://user:pw@prod/db",
  "migrationsDir": "/migrations", "shadowDsn": "postgresql://user:pw@shadow/db",
  "sarif": true
}'
```

---

## 14. Configuration

**Environment variables** (see `docker-compose.yml`):

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_LLM_PROVIDER` | `ollama` | LLM provider (the platform's provider abstraction) |
| `DEFAULT_LLM_MODEL`    | `neural-chat-local:latest` | default model |
| `OLLAMA_BASE_URL`      | `http://ollama:11434` | Ollama endpoint |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | optional cloud LLM keys |

The LLM is reached via the SDK's standalone gateway (`env_llm_port()`); when none is configured the
module runs fully offline (templates + heuristics + deterministic Core).

**`/run` settings** (per request): `database_type` (`sql`|`nosql`|`vector`), `driver`
(`postgresql`|`mysql`|`mongodb`|`sqlite`), `ai_suggestions` (default `true`), `import_type` (for
`existing_database`), and `feature_request` (may be threaded via `ctx.settings`).

**Driver support note:** the simple-layer exporters support several drivers; the **Core** SQL emitter,
importer and drift are **Postgres-only** (Milestone scope). Live import/drift require a reachable
Postgres DSN and the `psycopg` driver.

**Container:** `Dockerfile` builds on `python:3.12-slim`, installs the SDK then `requirements.txt`,
copies `app/` + `frontend/`, runs as a non-root user, exposes `9107`, and health-checks `/health`.
The build context **must be the repository root** (it needs `packages/module-sdk-python`).

---

## 15. Dependencies

- **Python 3.12** (the test `conftest.py` fails loudly on anything older or on a missing dependency).
- **`aiarch_module_sdk`** — the platform's Module Protocol SDK (provides FastAPI, pydantic, httpx,
  structlog, `build_module_app`, `LLMClient`). Installed from `packages/module-sdk-python`.
- **`uvicorn[standard]`** — ASGI server.
- **`jsonschema`** — structural validation of `schema_json` against the bundled JSON Schema. *(Required
  at import time — without it the service fails to boot.)*
- **`psycopg[binary]`** — Postgres driver for brownfield import / shadow-DB apply / drift. *(Imported
  lazily; only needed when actually touching a database.)*
- **Frontend (CDN, no install):** React 18, ReactDOM, React Flow 11, Mermaid 11.

---

## 16. Known limitations

- **Two schema models.** The simple `DatabaseSchema` and the Core `schema_json` are separate; there is
  no automatic bridge between them, so the canvas/`/design`/`/export` flow and the `/design/*` Core
  pipeline operate on different representations.
- **Design sessions are in-memory.** They do not survive a restart and are not shared across replicas;
  a persistent store is not yet implemented.
- **Core SQL emitter / importer / drift are Postgres-only** (Milestone scope). Other drivers exist only
  in the simple-layer exporters.
- **Migrations "leg" is raw SQL only.** Three-way drift's Leg B applies raw-SQL files to a shadow DB (or
  uses a prepared shadow DSN); framework-specific migration runners (Laravel/Prisma/Alembic) are not yet
  implemented. There is **no ORM-model AST scanner** (that would be a future fourth leg).
- **Regex-based simple-layer importers** (`SQLParser`, `LaravelMigrationParser`) are lightweight and
  skip what they can't parse; the production-grade importer is the Core `introspect_postgres`.
- **No auto-fix and no merge.** Drift reports + suggests only; schema branching is detect-only. The
  seeder produces data/SQL but never applies it to a database.
- **Seeder check constraints.** The seeder respects FK/unique/nullable/enum/state-machine constraints,
  but complex `CHECK` constraints are surfaced as warnings rather than fully solved.
- **No real-time collaboration**, and no admin UI in this service (the Panel owns user-facing UI).
- **Composite foreign keys** are handled best-effort on import.

---

## 17. Future improvements

- A **bridge/adapter** between `DatabaseSchema` and `schema_json` so the canvas and the Core pipeline
  share one representation.
- **Persistent design sessions** (DB-backed `SessionStore`) for multi-replica/production use.
- **Multi-driver Core** — extend the SQL emitter, importer and risk rules to MySQL/SQLite/etc.
- **Framework migration runners** for Leg B (apply Laravel/Prisma/Alembic migrations to the shadow DB
  via adapters) and an **ORM-model AST scanner** as a fourth drift leg.
- **Phase-2 generators** driven by the Core Type System (API / Form / Admin / GDPR projections). The
  **Seeder** (Milestone 3) already ships — it fabricates a real referenced UUID, not an integer, for a
  UUID FK; remaining generators would join it.
- **Schema merge / branching** beyond conflict detection.
- **Richer reconciliation** (auto-generate the migration for `design_ahead_of_code`, propose the
  `import_to_design` edit for manual prod changes) — still behind the human gate.

---

## 18. Testing

Run from the **module directory** with the SDK virtualenv (the repo's default `python` is 3.10 with no
deps and will be rejected loudly by `conftest.py`):

```bash
cd services/modules/visual-database-designer
../../../packages/module-sdk-python/.venv/Scripts/python.exe -m pytest -q
```

- **Unit tests:** `tests/core/` (one file per engine) + `tests/test_module.py`.
- **Conformance kits** (marked `conformance`): `tests/milestones/test_m1_greenfield.py` (positive path,
  4 negative gate tests, determinism, SQL snapshot), `tests/milestones/test_m2_brownfield.py` (import
  snapshot + determinism, the **emit→apply→import** round-trip, all drift categories, reconcile), and
  `tests/milestones/test_m3_seeder.py` (FK-type proof, state-machine consistency, determinism, and the
  **schema→migration→apply→seed→insert** full loop on a real server).
- **Live-Postgres tests** (marked `live_postgres`) are opt-in but must pass once on a real server to
  count as proven — set `VDB_TEST_POSTGRES_DSN`, e.g.:
  ```bash
  docker run --rm -d --name vdb-pg -e POSTGRES_PASSWORD=vdb -p 5433:5432 postgres:16
  VDB_TEST_POSTGRES_DSN="postgresql://postgres:vdb@localhost:5433/postgres" \
    ../../../packages/module-sdk-python/.venv/Scripts/python.exe -m pytest -m live_postgres -v
  ```
- Marker selection: `-m conformance`, `-m "not conformance"`, `-m live_postgres`. Lint with
  `ruff check app tests`.
```
Current status: 284 passed + 6 skipped (live, green when run on a server), ruff clean.
```
