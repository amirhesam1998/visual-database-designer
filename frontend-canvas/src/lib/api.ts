import type {
  DiffResult,
  DriftReport,
  ImportResult,
  InsightReport,
  RenderModel,
  RiskReport,
  SemanticTypeInfo,
  ValidationReport,
} from "./types";
import type { PresentationNode, SchemaDoc } from "./schema";

// The data path stays one-way and engine-authoritative (spec §6). Milestone 1 fetched the read-only
// render projection; Milestone 2 adds the *edit* round-trip — but the front-end still never decides
// anything: it sends the working schema to the engine and renders what comes back.
//
//   edit → mutate local schema_json (structural only)
//        → POST /design/render   (resolved view: FK types, etc.)
//        → POST /core/validate   (findings shown inline)
//        → POST /design/presentation (layout save; NOT a schema change)

export interface RenderRequest {
  /** Render a stored design session's current draft. */
  sessionId?: string;
  /** Render a raw schema_json document (e.g. from /design/import, a handoff or local edits). */
  schemaJson?: unknown;
}

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const err = await res.json();
      detail = err.detail || err.error || detail;
      if (err.hint) detail += ` — ${err.hint}`;  // e.g. the localhost/host.docker.internal tip (§4)
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${path} failed: ${detail}`);
  }
  return (await res.json()) as T;
}

export async function fetchRenderModel(req: RenderRequest, signal?: AbortSignal): Promise<RenderModel> {
  const body: Record<string, unknown> = {};
  if (req.sessionId) body.sessionId = req.sessionId;
  if (req.schemaJson !== undefined) body.schema_json = req.schemaJson;
  return postJson<RenderModel>("/design/render", body, signal);
}

/** Engine validation of the working document (spec §0). Defensive defaults keep a malformed/offline
 *  response from breaking the editor — an absent report simply shows no findings. */
export async function validateSchema(doc: SchemaDoc, signal?: AbortSignal): Promise<ValidationReport> {
  const raw = await postJson<{ report?: ValidationReport }>("/core/validate", { schema: doc }, signal);
  const report = raw.report ?? (raw as unknown as ValidationReport);
  return {
    valid: report?.valid ?? true,
    findings: Array.isArray(report?.findings) ? report.findings : [],
    summary: report?.summary ?? {},
  };
}

/** Deterministic design-assistant analysis of the working document (`POST /design/insights`,
 *  intelligence milestone). The engine owns the analysis (index advice, design warnings, sensitive-
 *  field detection); the canvas only renders the findings and applies their actions through the normal
 *  edit path (spec §0/§5). Defensive defaults keep a malformed/offline response from breaking the UI. */
export async function fetchInsights(doc: SchemaDoc, signal?: AbortSignal): Promise<InsightReport> {
  const r = await postJson<Partial<InsightReport>>("/design/insights", { schema: doc }, signal);
  return {
    insights: Array.isArray(r.insights) ? r.insights : [],
    summary: r.summary ?? {},
  };
}

/** The semantic-type catalogue for the editor's type dropdown (`GET /core/types`, spec §1/§3). */
export async function fetchTypes(signal?: AbortSignal): Promise<SemanticTypeInfo[]> {
  const res = await fetch(`${BASE}/core/types`, { signal });
  if (!res.ok) throw new Error(`types failed: ${res.status}`);
  const body = (await res.json()) as { types?: SemanticTypeInfo[] };
  return Array.isArray(body.types) ? body.types : [];
}

/** Persist canvas layout (spec §4). Layout is display-only; this is intentionally separate from the
 *  edit/validate cycle and never counts as a schema change. Returns the layout-merged document. */
export async function savePresentation(
  args: { sessionId?: string; schemaJson: SchemaDoc; nodes: PresentationNode[] },
  signal?: AbortSignal,
): Promise<SchemaDoc> {
  const body: Record<string, unknown> = { nodes: args.nodes, schema_json: args.schemaJson };
  if (args.sessionId) body.sessionId = args.sessionId;
  const res = await postJson<{ schema_json: SchemaDoc }>("/design/presentation", body, signal);
  return res.schema_json;
}

/** Fetch a design session's raw, editable schema_json (so a `?sessionId=` canvas can be edited). */
export async function fetchSessionDoc(sessionId: string, signal?: AbortSignal): Promise<SchemaDoc> {
  const res = await fetch(`${BASE}/design/sessions/${encodeURIComponent(sessionId)}`, { signal });
  if (!res.ok) throw new Error(`session load failed: ${res.status}`);
  const body = (await res.json()) as { schema_json: SchemaDoc };
  return body.schema_json;
}

// ---- Diff + Approve (Milestone 3) -------------------------------------------------------------
// The canvas only *shows* the diff/risk and *calls* the gate — the engine owns every decision (§0).

/** The typed operation list between two schemas (`POST /core/diff`). Presentation is ignored by the
 *  engine, so a table move yields no operations (spec §1/§5). Defensive defaults keep a malformed
 *  response from breaking the canvas tint/diff panel. */
export async function diffSchema(from: SchemaDoc, to: SchemaDoc, signal?: AbortSignal): Promise<DiffResult> {
  const r = await postJson<Partial<DiffResult>>("/core/diff", { from, to }, signal);
  return {
    operations: Array.isArray(r.operations) ? r.operations : [],
    changelog: Array.isArray(r.changelog) ? r.changelog : [],
    stats: r.stats ?? { added: 0, removed: 0, changed: 0, renamed: 0 },
    colored: Array.isArray(r.colored) ? r.colored : [],
    notes: Array.isArray(r.notes) ? r.notes : [],
  };
}

/** Migration risk for the pending changes (`POST /core/risk`) — shown before approve (spec §3). */
export async function riskSchema(from: SchemaDoc, to: SchemaDoc, signal?: AbortSignal): Promise<RiskReport> {
  return postJson<RiskReport>("/core/risk", { from, to }, signal);
}

/** A non-throwing POST so the gate's 409 (blocked) body is readable, not an exception. */
async function postResult(path: string, body: unknown): Promise<{ status: number; body: Record<string, unknown> }> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  let parsed: Record<string, unknown> = {};
  try {
    parsed = (await res.json()) as Record<string, unknown>;
  } catch {
    /* empty body */
  }
  return { status: res.status, body: parsed };
}

// The approval gate lives entirely in the engine (AD-5). The canvas drives it through the *existing*
// session endpoints: a brownfield session whose baseline is the canvas's base, the working document
// applied as the draft, then validate → submit → approve. No new engine logic (spec §0/§6).
export async function createBaselineSession(baseline: SchemaDoc): Promise<string> {
  const body = await postJson<{ sessionId: string }>("/design/sessions", {
    mode: "brownfield",
    schema_json: baseline,
  });
  return body.sessionId;
}

export async function applySessionSchema(sessionId: string, schemaJson: SchemaDoc): Promise<void> {
  await postJson(`/design/sessions/${encodeURIComponent(sessionId)}/apply-suggestion`, { schema_json: schemaJson });
}

export async function validateSession(sessionId: string): Promise<{ state: string; report: Record<string, unknown> }> {
  return postJson(`/design/sessions/${encodeURIComponent(sessionId)}/validate`, {});
}

export async function submitSession(sessionId: string): Promise<void> {
  await postJson(`/design/sessions/${encodeURIComponent(sessionId)}/submit`, {});
}

// ---- Code generation (unify spec phase 2 §2) --------------------------------------------------
// The canvas never generates code itself: `sql` comes from the deterministic Core (diff→emitter) and
// `model`/`crud`/`schema` come from the proven legacy generators behind the server-side bridge. The
// front-end only picks a kind/framework and shows the returned text (spec §3, no logic in the front-end).

// `sql`/`openapi` + the bridge kinds (`model`/`crud`/`schema`) plus deterministic text exports
// (`yaml`/`dbml`/`jsonschema`/`datadict`) — all generated by the engine, never the browser.
export type CodeKind = "sql" | "model" | "crud" | "schema" | "yaml" | "dbml" | "jsonschema" | "datadict";

export interface CodeFrameworks {
  sql: string[];
  model: string[];
  crud: string[];
  crudMethods: string[];
  schema: string[];
  text?: string[];
}

export async function fetchCodeFrameworks(signal?: AbortSignal): Promise<CodeFrameworks> {
  const res = await fetch(`${BASE}/design/code/frameworks`, { signal });
  if (!res.ok) throw new Error(`code frameworks failed: ${res.status}`);
  return (await res.json()) as CodeFrameworks;
}

export async function generateCode(
  args: { schemaJson: SchemaDoc; kind: CodeKind; framework?: string; table?: string; methods?: string[]; driver?: string },
  signal?: AbortSignal,
): Promise<string> {
  const body: Record<string, unknown> = { schema_json: args.schemaJson, kind: args.kind };
  if (args.framework) body.framework = args.framework;
  if (args.table) body.table = args.table;
  if (args.methods) body.methods = args.methods;
  if (args.driver) body.driver = args.driver;
  const res = await postJson<{ content: string }>("/design/code", body, signal);
  return res.content;
}

/** OpenAPI is engine-native (Milestone 4) — generated straight from the schema (spec phase 2 §1). */
export async function generateOpenApi(schemaJson: SchemaDoc, signal?: AbortSignal): Promise<string> {
  const res = await postJson<{ openapi: unknown }>("/design/api/contract", { schema_json: schemaJson }, signal);
  return JSON.stringify(res.openapi, null, 2);
}

// ---- Generate schema from a PRD (unify spec phase 2 §1 — greenfield session path) ---------------
// The engine owns generation (deterministic core + optional LLM suggest). The canvas only kicks off a
// greenfield session, asks for a suggestion and applies it — then renders the result like any schema.
export async function generateSchemaFromPrd(prd: string, signal?: AbortSignal): Promise<SchemaDoc> {
  const created = await postJson<{ sessionId: string }>("/design/sessions", { mode: "greenfield", prd }, signal);
  const sid = created.sessionId;
  const suggestion = await postJson<{ suggestion?: SchemaDoc }>(
    `/design/sessions/${encodeURIComponent(sid)}/suggest`,
    {},
    signal,
  );
  const schema = suggestion.suggestion;
  if (!schema) throw new Error("the engine returned no schema suggestion");
  const applied = await postJson<{ schema_json: SchemaDoc }>(
    `/design/sessions/${encodeURIComponent(sid)}/apply-suggestion`,
    { schema_json: schema },
    signal,
  );
  return applied.schema_json;
}

// ---- Import an existing database + drift (database-connection milestone §1/§2) ----------------
// Both are engine endpoints (M2). The canvas never parses SQL or computes drift itself: a live DSN
// or a SQL file goes to `/design/import`, the working schema + a live DSN go to `/design/drift`. A
// uuid FK comes back as uuid because the engine reads the type from the database (golden rule).

/** Introspect a live database into a schema_json (`POST /design/import {dsn}`). ``driver`` selects
 *  the database dialect (postgres | mysql); the engine reads types from the DB so a uuid FK survives. */
export async function importLiveDatabase(
  dsn: string, name?: string, driver?: string, signal?: AbortSignal,
): Promise<ImportResult> {
  return postJson<ImportResult>("/design/import", { dsn, ...(name ? { name } : {}), ...(driver ? { driver } : {}) }, signal);
}

/** Import a SQL/DDL dump via the engine's shadow-database path (`POST /design/import {sql}`). The
 *  front-end uploads the text only — applying it to a temporary shadow DB + introspecting is the
 *  engine's job (no SQL parsing in the browser, spec §2). ``driver`` picks the shadow DB's dialect. */
export async function importSqlDump(
  sql: string, name?: string, driver?: string, signal?: AbortSignal,
): Promise<ImportResult> {
  return postJson<ImportResult>("/design/import", { sql, ...(name ? { name } : {}), ...(driver ? { driver } : {}) }, signal);
}

/** Three-way drift of the working design against a live database (`POST /design/drift`). Report-only
 *  (AD-5) — the engine categorises every difference; the canvas just shows it (spec §3). */
export async function driftAgainstDatabase(
  designed: SchemaDoc,
  liveDsn: string,
  driver?: string,
  signal?: AbortSignal,
): Promise<DriftReport> {
  const r = await postJson<Partial<DriftReport>>(
    "/design/drift",
    { designed, liveDsn, ...(driver ? { driver } : {}) },
    signal,
  );
  return {
    reconcile: r.reconcile ?? { matched: 0, ambiguous: [] },
    drift: Array.isArray(r.drift) ? r.drift : [],
    summary: r.summary ?? {},
    exitCode: r.exitCode ?? 0,
  };
}

/** Drive the gate's approve. Returns the raw status + body so the store can map blocked reasons. */
export async function approveSession(
  sessionId: string,
  args: { approvedBy: string; acknowledgeCritical: boolean },
): Promise<{ status: number; body: Record<string, unknown> }> {
  return postResult(`/design/sessions/${encodeURIComponent(sessionId)}/approve`, {
    approvedBy: args.approvedBy,
    acknowledgeCritical: args.acknowledgeCritical,
  });
}
