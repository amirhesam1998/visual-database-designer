# Canvas — Visual Database Designer (complete: view → edit → diff → approve)

A React Flow workbench for a database map — tables, fields and relations. The full three-milestone
cycle is in place: **view** it, **edit** it (create/rename/delete tables and fields, change a field's
*semantic* type, draw relations, reposition), **see the diff** of your changes, and **approve** them
through the engine's gate — all without leaving the canvas.

> **The canvas is a tool, not a decision-maker.** No database logic lives here — no validation, type
> resolution, diff, risk or approval. Every edit only mutates the local `schema_json` *structurally*,
> then goes back through the engine: `POST /design/render` re-resolves types (so a foreign-key column
> shows its referenced primary key's physical type — a uuid FK renders as `uuid`, never an int),
> `POST /core/validate` returns findings shown inline, `POST /core/diff` produces the change list, and
> the approval gate (`/design/sessions/*`) makes the decision. A relation can never be created without
> its foreign-key field, so the recurring "incomplete relation" bug is impossible from the UI (§2/§3).

## Stack
- **React + React Flow** (canvas, zoom/pan/minimap/controls, connect-to-draw-relation).
- **Tailwind + shadcn-style** components, light/dark theme via CSS variables (respects the OS
  preference, switches with no reload).
- **dagre** layered auto-layout when a schema has no saved `presentation` positions.
- **zustand** for the single, centralized state (the editable document + the engine-resolved view).

## Data path (one-way, engine-authoritative — spec §0/§6)
```
edit → mutate local schema_json (structural only)
     → POST /design/render        (resolved view: FK types, etc.)
     → POST /core/validate        (findings shown inline)
     → POST /core/diff            (change list base→working; canvas tint + diff panel)
     → POST /design/presentation  (layout save; NOT a schema change)

approve → /design/sessions (brownfield baseline) → apply → validate → submit → approve
```
Layout (table positions) is saved separately and never counts as a schema change — the "unsaved
changes" indicator and the diff ride on the schema signature, which ignores `presentation`.

## Editing
- **Panel (click a table):** rename table, edit description/domain, add/rename/delete fields, change
  the **semantic** type (from the Type System dropdown, `GET /core/types`), attach a reusable enum to
  an enum/status field, toggle PK/nullable, manage explicit/composite **indexes**, a **timestamps**
  and **soft-delete** toggle (which add real `created_at`/`updated_at`/`deleted_at` datetime columns,
  not magic flags), **duplicate** the table (fresh Stable IDs), or delete it.
- **Canvas:** drag from one table's handle to another to create a relation (a dialog requires the
  FK field + cardinality); double-click a table/field name to rename; use the toolbar to add a table;
  select + <kbd>Delete</kbd> to remove; drag to reposition.
- **Toolbar:** **Generate** a schema from a description (greenfield session → suggest → apply),
  **Import** an existing database (live or SQL file), add table, manage **Enums**, undo/redo, zoom
  in/out/fit/**1:1**, an "unsaved" indicator, a validation summary, the **Insights** panel, the
  **Code** panel, **Compare DB** (drift), **Changes**, and **Approve**.

## Open an existing database (import & connect)
- **Import** opens a dialog with a **Database** selector (**PostgreSQL** or **MySQL/MariaDB**) and two
  engine-backed sources (`/design/import`): a **live** connection (connection string or
  host/port/db/user/pass) or a **SQL/DDL file** (applied to a server-side *shadow* database, then
  introspected — the browser never parses SQL). A uuid foreign key comes back as `uuid` because the
  engine reads the type from the database — on MySQL a `CHAR(36)` key is recognised as uuid.
- Ambiguous reverse-inferences the engine flags are shown for you to confirm before loading (AD-5);
  nothing is auto-applied. The imported map is a brownfield baseline — edit it, then diff/approve.
- The connection string is used once and never persisted in the UI.

## Compare with a database (drift)
- **Compare DB** sends the working design + a live connection to `/design/drift` and renders the
  engine's three-way report (design ↔ migrations ↔ live): what's only in your design, only in the
  database, or has a different type. It also tints the canvas (design-only = green, type drift =
  yellow). Report-only — nothing is ever written back to the database.

## Insights (design assistant)
- **Insights** sends the working schema to `/design/insights` and shows the engine's deterministic
  analysis: index advice, design warnings and sensitive-field detection. The panel keeps the two
  natures apart (spec §0) — **Issues** are certain facts (no primary key, an FK whose type doesn't
  match the key it references, a redundant index); **Suggestions** are heuristic guesses (a column
  whose *name* looks sensitive). Each finding shows a severity and a plain-language "why".
- Findings with an action expose an **Add index** / **Mark sensitive** button. Clicking it makes the
  *same* structural edit you could make by hand — it goes through validate + diff and is undoable;
  nothing is auto-applied (AD-5). The finding disappears on the next analysis because the schema
  changed. Flagged tables/fields also get a small badge on the canvas so you see them in context.

## Generate from a description & code generation
- **Generate** sends a product description to the engine (`/design/sessions` greenfield + `suggest` +
  `apply-suggestion`) and loads the suggested schema — generation is the engine's, not the browser's.
- **Code** panel produces SQL DDL and OpenAPI 3.1 natively from the engine, plus ORM models, CRUD
  controllers and framework schema exports through the server-side bridge (`POST /design/code`). For
  SQL it offers a **Database** dropdown (**PostgreSQL / MySQL**) so the DDL matches the target dialect —
  same `schema_json`, different output (a uuid PK is `uuid` on Postgres, `CHAR(36)` on MySQL, and the FK
  column follows). The bridge preserves FK physical types and semantic types in translation.
- The same panel also offers deterministic documentation/interchange exports — **YAML, DBML
  (dbdiagram.io), JSON Schema and a Markdown data dictionary** (with PII/sensitive marks) — generated
  by the engine from `schema_json` with resolved types. Every artifact has **Copy** and **Download**.
- **Export ERD** (toolbar) saves the current diagram as **SVG / PNG / PDF** — captured client-side
  from the React Flow canvas (the only client-side export; display-only, never a schema change).
- The Mermaid ERD tab from the old canvas was intentionally dropped — the canvas itself is a live ERD.

## Diff & approve (Milestone 3)
- **Changes** opens the diff panel: the engine's operation list rendered with the standard colours
  (green=add, red=remove, yellow=change, blue=rename). The same colours tint the changed tables,
  fields and relations on the canvas. Moving a table never appears (the engine ignores layout).
- **Approve** is enabled only once the engine has validated the changes with no errors. The dialog
  shows the migration **risk** from `/core/risk`; a **critical** operation (e.g. dropping a table)
  must be explicitly acknowledged before the gate will accept it. On success the version is locked
  and becomes the new base the next changes diff against. Every decision is the engine's.

## Develop
```bash
cd frontend-canvas
npm install
npm run dev          # Vite dev server; proxies /design and /core to the module on :9107
```
Open with `?sessionId=<id>` to edit a live design session; with no query it loads a bundled sample
schema (still through the engine, so types resolve correctly).

## Build & test
```bash
npm run build        # type-check + production build into dist/ (served by FastAPI at /designer)
npm test             # Vitest + Testing Library (schema mutations, engine-validated editing, FK guard)
```

## Served by the module
The production build in `dist/` is served at **`GET /designer/`** — the sole visual reference. (The
legacy no-build `/canvas` SPA has been removed; all of its features now live here, engine-connected.)
