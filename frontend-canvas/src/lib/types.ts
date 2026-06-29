// The render contract returned by `POST /design/render` (see app/routes.py `_render_model`).
// This is the *only* shape the canvas consumes — all database logic stays in the engine (spec §0).

export type RelationType =
  | "one_to_one"
  | "one_to_many"
  | "many_to_one"
  | "many_to_many"
  | "polymorphic"
  | "self"
  | "has_many_through"
  | "embedded";

export interface RenderField {
  id: string;
  name: string;
  semanticType: string;
  /** Resolved physical type (FK columns already inherit the referenced PK's type, e.g. `uuid`). */
  physicalType: string;
  nullable: boolean;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
  /** True when the Type System marks this field as personally-identifiable / sensitive. */
  pii: boolean;
  sensitivity: string | null;
  enumId: string | null;
  comment: string | null;
}

export interface RenderTable {
  id: string;
  name: string;
  comment: string | null;
  kind: string | null;
  fields: RenderField[];
}

export interface RenderRelation {
  id: string;
  type: RelationType;
  fromTableId: string;
  toTableId: string | null;
  foreignKeyFieldId: string | null;
  onDelete: string | null;
  onUpdate: string | null;
}

export interface RenderEnum {
  id: string;
  name: string;
  values: string[];
}

export interface PresentationNode {
  tableId: string;
  x: number;
  y: number;
  color?: string | null;
  group?: string | null;
}

export interface RenderModel {
  meta: Record<string, unknown>;
  tables: RenderTable[];
  relations: RenderRelation[];
  enums: RenderEnum[];
  presentation: { nodes: PresentationNode[] };
  hasLayout: boolean;
}

// ---- Validation (Milestone 2) -----------------------------------------------------------------
// The engine is the only authority on correctness (spec §0). After every edit the canvas sends the
// working schema to `/core/validate` and renders these findings inline — it never decides validity
// itself. Field names mirror the engine's `model_dump()` (snake_case).

export type Severity = "error" | "warning" | "suggestion" | "security" | "performance";

export interface ValidationFinding {
  rule_id: string;
  severity: Severity;
  message: string;
  path: string;
  entity_id: string | null;
  fix: string | null;
}

export interface ValidationReport {
  valid: boolean;
  findings: ValidationFinding[];
  summary: Record<string, number>;
}

/** A semantic type offered in the editor's type dropdown (from `GET /core/types`, spec §1/§3). */
export interface SemanticTypeInfo {
  id: string;
  category: string;
  physical: Record<string, unknown>;
  pii: boolean;
}

/** Offline fallback for the type dropdown — the live list comes from `GET /core/types`. The user
 *  always picks a *semantic* type (spec §1/§3); the engine maps it to a physical type, never the UI. */
export const FALLBACK_SEMANTIC_TYPES = [
  "string", "text", "email", "url", "slug", "password", "uuid", "boolean",
  "integer", "big_integer", "decimal", "money", "percentage", "rating",
  "date", "datetime", "time", "enum", "status", "json", "foreign_key",
];

// ---- Diff + Approve (Milestone 3) -------------------------------------------------------------
// All of this comes from the engine — the canvas computes no diff and decides no approval (spec §0).
// `/core/diff` returns camelCase operations (op_dicts); `/core/risk` + the approval gate return
// snake_case (model_dump). We mirror each verbatim so nothing is re-derived in the front-end.

/** Standard Diff Engine colours: green=add, red=drop, yellow=change, blue=rename (spec §1). */
export type ChangeColor = "green" | "red" | "yellow" | "blue";

export interface DiffOperation {
  op: string;
  layer?: string;
  tableId?: string;
  fieldId?: string;
  entityId?: string;
  name?: string;
  from?: unknown;
  to?: unknown;
  field?: Record<string, unknown>;
  details?: Record<string, unknown>;
}

export interface DiffResult {
  operations: DiffOperation[];
  changelog: string[];
  stats: { added: number; removed: number; changed: number; renamed: number };
  colored: { color: string; text: string }[];
  notes: string[];
}

export interface RiskOperation {
  op: string;
  target: string | null;
  level: "safe" | "low" | "medium" | "high" | "critical";
  dimensions: string[];
  reversible: boolean;
  requires_backup: boolean;
  explanation: { fa?: string; en?: string };
  safe_plan: string[];
}

export interface RiskReport {
  driver: string;
  operations: RiskOperation[];
  summary: Record<string, number>;
  max_level: "safe" | "low" | "medium" | "high" | "critical";
  exit_code: number;
  checklist: string[];
}

export interface BlockingOp {
  op: string;
  target?: string | null;
  level: string;
}

/** The result of driving the engine approval gate (spec §2/§3) — never a front-end decision. */
export type ApproveResult =
  | { status: "approved"; schemaVersion?: string; checksum?: string }
  | { status: "validation_error"; summary?: Record<string, number> }
  | { status: "critical_risk"; blocking: BlockingOp[] }
  | { status: "error"; message: string };

// ---- Database connection: import + drift (database-connection milestone) ----------------------
// The engine owns introspection and drift (M2 — /design/import, /design/drift); the canvas only
// surfaces them. A uuid FK comes back as uuid because the engine reads the type from the database
// (the lesson of the whole project); the front-end parses no SQL and decides no types (golden rule).

/** A low-confidence reverse-inference the engine flagged for the human to confirm (AD-5). */
export interface ImportSuggestion {
  table?: string;
  column?: string;
  relation?: string;
  physicalType?: string;
  suggestedType?: string;
  llmSuggestion?: string;
  confidence?: number;
  reason?: string;
}

/** The result of `POST /design/import` — a Stable-ID schema_json plus inference/validation context. */
export interface ImportResult {
  schema_json: SchemaDocLike;
  inference?: { confident: number; ambiguous: number; suggestions: ImportSuggestion[] };
  validation?: { summary?: Record<string, number>; structuralErrors?: string[] };
}

/** Minimal placeholder so types.ts doesn't import the schema module (kept structurally opaque here). */
export type SchemaDocLike = Record<string, unknown>;

/** One three-way drift entry from `POST /design/drift` (designed ↔ migrations ↔ live, spec M2 §2). */
export interface DriftEntry {
  entity: string; // "users" or "users.email"
  kind: string; // table | column | type
  status: { designed?: boolean; migrations?: boolean; live?: boolean };
  category: string;
  severity: string;
  detail: string | null;
  suggestion: { action?: string } | null;
}

export interface DriftReconcile {
  matched: number;
  ambiguous: { entity: string; candidates: string[]; confidence: number; reason: string }[];
  canonical_ids?: Record<string, string>;
}

export interface DriftReport {
  reconcile: DriftReconcile;
  drift: DriftEntry[];
  summary: Record<string, number>;
  exitCode: number;
}

// ---- Insights: design assistant (intelligence milestone) --------------------------------------
// The engine analyses the schema deterministically and returns findings; the canvas only shows them
// and, on demand, applies a finding's action through the normal edit path (golden rule, spec §0/§5).
// Field names mirror the engine's `model_dump()` (snake_case), like the validation findings.

export type InsightKind = "fact" | "suggestion";
export type InsightSeverity = "error" | "warning" | "info";
export type InsightCategory = "index" | "design" | "privacy";

/** A structured, engine-applicable action (e.g. add an index, mark a field sensitive). The canvas maps
 *  `type` onto an existing structural edit; it never decides *how* to apply it (spec §5). */
export interface InsightAction {
  type: "add_index" | "mark_sensitive" | string;
  label: string;
  table_id?: string | null;
  field_id?: string | null;
  columns?: string[] | null;
  unique?: boolean | null;
  sensitivity?: string | null;
}

export interface Insight {
  rule_id: string;
  category: InsightCategory;
  kind: InsightKind;
  severity: InsightSeverity;
  title: string;
  why: string;
  path: string;
  entity_id: string | null;
  table_id: string | null;
  field_id: string | null;
  fix: string | null;
  action: InsightAction | null;
}

export interface InsightReport {
  insights: Insight[];
  summary: Record<string, number>;
}

/** Cardinality glyphs shown on relation edges (read-only direction marker, spec §2). */
export const CARDINALITY: Record<string, string> = {
  one_to_one: "1 — 1",
  one_to_many: "1 — ∞",
  many_to_one: "∞ — 1",
  many_to_many: "∞ — ∞",
  polymorphic: "poly",
  self: "self",
  has_many_through: "1 — ∞ —through",
  embedded: "embedded",
};
