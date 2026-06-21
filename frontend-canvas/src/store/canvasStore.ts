import { create } from "zustand";
import {
  applySessionSchema,
  approveSession,
  createBaselineSession,
  diffSchema,
  driftAgainstDatabase,
  fetchRenderModel,
  fetchSessionDoc,
  fetchTypes,
  savePresentation,
  submitSession,
  validateSchema,
  validateSession,
  type RenderRequest,
} from "@/lib/api";
import { neighboursOf } from "@/lib/graph";
import * as edit from "@/lib/schema";
import type { NewRelation, SchemaDoc } from "@/lib/schema";
import { applyTheme, initialTheme, type Theme } from "@/lib/theme";
import type {
  ApproveResult,
  BlockingOp,
  ChangeColor,
  DiffResult,
  DriftReport,
  RelationType,
  RenderModel,
  RenderTable,
  SemanticTypeInfo,
  ValidationFinding,
  ValidationReport,
} from "@/lib/types";

/** The identity recorded against an approval. The gate requires a non-empty approver (AD-5). */
const APPROVED_BY = "canvas-user";

/** Arguments for drawing a relation on the canvas. The FK field is resolved to either an existing
 *  column or a freshly-created one — but it is ALWAYS present, so an incomplete relation (the
 *  recurring project bug) can never be produced from the UI (spec §2/§3). */
export interface ConnectArgs {
  fromTableId: string;
  toTableId: string;
  type: RelationType;
  fkFieldId?: string; // an existing column on the source table
  newFkFieldName?: string; // …or create a new foreign_key column with this name
  onDelete?: string | null;
}

// Centralized map state (spec §6). Milestone 2 turns this into the editing hub: it now owns the
// editable `doc` (schema_json) AND the engine-resolved `model`/`validation`. The data path stays
// one-way and engine-authoritative — a mutation only edits `doc` structurally, then `refresh()`
// asks the engine to re-resolve and re-validate. The front-end decides nothing (spec §0).

export type LoadStatus = "idle" | "loading" | "ready" | "error";

const MAX_HISTORY = 50;

interface CanvasState {
  // engine-resolved view + working document
  model: RenderModel | null;
  doc: SchemaDoc | null;
  validation: ValidationReport | null;
  types: SemanticTypeInfo[];

  status: LoadStatus;
  error: string | null;
  validating: boolean;

  // editing context
  editable: boolean;
  sessionId: string | null;
  /** Signature of the last saved/loaded schema (presentation excluded) — basis for "unsaved" (spec §5). */
  savedSignature: string;
  past: SchemaDoc[];
  future: SchemaDoc[];

  // diff + approve (Milestone 3) — all engine-sourced
  /** The base the canvas diffs against (the loaded schema; the approved schema after an approve). */
  baseline: SchemaDoc | null;
  /** Engine diff base→working, refreshed after every edit (spec §1). null until first computed. */
  diff: DiffResult | null;
  approving: boolean;
  approved: { schemaVersion?: string; checksum?: string } | null;

  // drift against a real database (database-connection milestone §3) — engine-sourced, report-only
  drift: DriftReport | null;
  driftBusy: boolean;

  selectedTableId: string | null;
  hoveredTableId: string | null;
  search: string;
  theme: Theme;

  load: (req: RenderRequest) => Promise<void>;
  refresh: () => Promise<void>;
  mutate: (fn: (doc: SchemaDoc) => SchemaDoc) => Promise<void>;
  commitPositions: (positions: Record<string, { x: number; y: number }>) => void;
  undo: () => Promise<void>;
  redo: () => Promise<void>;

  // typed edit actions (thin wrappers over the pure mutations in lib/schema)
  addTable: (name: string) => Promise<void>;
  removeTable: (tableId: string) => Promise<void>;
  duplicateTable: (tableId: string) => Promise<void>;
  updateTable: (tableId: string, patch: Partial<Pick<edit.SchemaTable, "name" | "domain" | "kind" | "comment">>) => Promise<void>;
  setTimestamps: (tableId: string, on: boolean) => Promise<void>;
  setSoftDelete: (tableId: string, on: boolean) => Promise<void>;
  addField: (tableId: string, field: { name: string; semanticType: string; nullable?: boolean }) => Promise<void>;
  updateField: (tableId: string, fieldId: string, patch: Partial<Pick<edit.SchemaField, "name" | "semanticType" | "nullable" | "isPrimaryKey" | "enumId">>) => Promise<void>;
  removeField: (tableId: string, fieldId: string) => Promise<void>;
  addRelation: (rel: NewRelation) => Promise<void>;
  removeRelation: (relationId: string) => Promise<void>;
  connect: (args: ConnectArgs) => Promise<void>;

  // reusable enums (engine logical.enums) + explicit indexes (engine physical.indexes)
  addEnum: (name: string, values?: string[]) => Promise<void>;
  updateEnum: (enumId: string, patch: { name?: string; values?: string[] }) => Promise<void>;
  removeEnum: (enumId: string) => Promise<void>;
  addIndex: (tableId: string, index: { columns: string[]; unique?: boolean; type?: string }) => Promise<void>;
  removeIndex: (indexId: string) => Promise<void>;

  /** Replace the whole working document (e.g. an engine-generated schema from a PRD), then re-render. */
  replaceDoc: (doc: SchemaDoc) => Promise<void>;

  /** Compare the working design against a live database via the engine (`/design/drift`, spec §3).
   *  Report-only: returns whether the call succeeded; every category is the engine's decision. */
  compareWithDatabase: (liveDsn: string) => Promise<{ ok: boolean; error?: string }>;
  clearDrift: () => void;

  /** Drive the engine approval gate (spec §2/§3). Returns the gate's verdict — never a UI decision. */
  runApprove: (acknowledgeCritical: boolean) => Promise<ApproveResult>;

  select: (tableId: string | null) => void;
  hover: (tableId: string | null) => void;
  setSearch: (q: string) => void;
  toggleTheme: () => void;
}

export const useCanvasStore = create<CanvasState>((set, get) => ({
  model: null,
  doc: null,
  validation: null,
  types: [],
  status: "idle",
  error: null,
  validating: false,
  editable: true, // Milestone 2: the canvas is now a workbench (spec §0)
  sessionId: null,
  savedSignature: "",
  past: [],
  future: [],
  baseline: null,
  diff: null,
  approving: false,
  approved: null,
  drift: null,
  driftBusy: false,
  selectedTableId: null,
  hoveredTableId: null,
  search: "",
  theme: initialTheme(),

  load: async (req) => {
    set({ status: "loading", error: null });
    try {
      const sessionId = req.sessionId ?? null;
      const doc = sessionId
        ? await fetchSessionDoc(sessionId)
        : (req.schemaJson as SchemaDoc);
      set({
        doc,
        baseline: doc, // the loaded schema is the base subsequent changes diff against (spec §1/§5)
        sessionId,
        savedSignature: edit.schemaSignature(doc),
        approved: null,
        drift: null,
        past: [],
        future: [],
      });
      await get().refresh();
      set({ status: "ready" });
      // The type catalogue is non-critical (the editor has a fallback) — never block on it.
      fetchTypes()
        .then((types) => types.length && set({ types }))
        .catch(() => undefined);
    } catch (e) {
      set({ status: "error", error: e instanceof Error ? e.message : String(e) });
    }
  },

  // Re-resolve, re-validate AND re-diff the working document through the engine (the only writer of
  // `model`/`diff`). The canvas computes none of these — it shows what the engine returns (spec §0).
  refresh: async () => {
    const { doc, baseline } = get();
    if (!doc) return;
    const model = await fetchRenderModel({ schemaJson: doc }); // throws → caller sets error state
    set({ model, validating: true });
    const [val, df] = await Promise.allSettled([
      validateSchema(doc),
      baseline ? diffSchema(baseline, doc) : Promise.resolve(null),
    ]);
    set({
      validating: false,
      ...(val.status === "fulfilled" ? { validation: val.value } : {}),
      ...(df.status === "fulfilled" ? { diff: df.value } : {}),
    });
  },

  mutate: async (fn) => {
    const current = get().doc;
    if (!current) return;
    const next = fn(current);
    set((s) => ({
      doc: next,
      past: [...s.past, current].slice(-MAX_HISTORY),
      future: [],
      drift: null, // an edit invalidates a drift report computed against the previous design
    }));
    await get().refresh();
  },

  // Layout is display-only and NOT a schema change (spec §4): update the doc locally and persist via
  // the dedicated endpoint, but never push it onto undo history or re-render (React Flow already
  // moved the node). The "unsaved changes" signature ignores presentation, so this stays clean.
  commitPositions: (positions) => {
    const { doc, sessionId } = get();
    if (!doc) return;
    const next = edit.setPositions(doc, positions);
    set({ doc: next });
    savePresentation({ sessionId: sessionId ?? undefined, schemaJson: next, nodes: edit.presentationNodes(next) }).catch(
      () => undefined,
    );
  },

  undo: async () => {
    const { past, doc } = get();
    if (!past.length || !doc) return;
    const previous = past[past.length - 1];
    set((s) => ({ doc: previous, past: s.past.slice(0, -1), future: [doc, ...s.future].slice(0, MAX_HISTORY) }));
    await get().refresh();
  },

  redo: async () => {
    const { future, doc } = get();
    if (!future.length || !doc) return;
    const nextDoc = future[0];
    set((s) => ({ doc: nextDoc, future: s.future.slice(1), past: [...s.past, doc].slice(-MAX_HISTORY) }));
    await get().refresh();
  },

  addTable: (name) => get().mutate((d) => edit.addTable(d, name).doc),
  removeTable: async (tableId) => {
    if (get().selectedTableId === tableId) set({ selectedTableId: null });
    await get().mutate((d) => edit.removeTable(d, tableId));
  },
  duplicateTable: async (tableId) => {
    const cur = get().doc;
    if (!cur) return;
    const { doc: next, tableId: newId } = edit.duplicateTable(cur, tableId);
    await get().mutate(() => next); // apply the precomputed copy (ids minted once, AD-1)
    set({ selectedTableId: newId });
  },
  updateTable: (tableId, patch) => get().mutate((d) => edit.updateTable(d, tableId, patch)),
  setTimestamps: (tableId, on) => get().mutate((d) => edit.setTimestamps(d, tableId, on)),
  setSoftDelete: (tableId, on) => get().mutate((d) => edit.setSoftDelete(d, tableId, on)),
  addField: (tableId, field) => get().mutate((d) => edit.addField(d, tableId, field).doc),
  updateField: (tableId, fieldId, patch) => get().mutate((d) => edit.updateField(d, tableId, fieldId, patch)),
  removeField: (tableId, fieldId) => get().mutate((d) => edit.removeField(d, tableId, fieldId)),
  addRelation: (rel) => get().mutate((d) => edit.addRelation(d, rel).doc),
  removeRelation: (relationId) => get().mutate((d) => edit.removeRelation(d, relationId)),
  addEnum: (name, values) => get().mutate((d) => edit.addEnum(d, name, values).doc),
  updateEnum: (enumId, patch) => get().mutate((d) => edit.updateEnum(d, enumId, patch)),
  removeEnum: (enumId) => get().mutate((d) => edit.removeEnum(d, enumId)),
  addIndex: (tableId, index) => get().mutate((d) => edit.addIndex(d, tableId, index).doc),
  removeIndex: (indexId) => get().mutate((d) => edit.removeIndex(d, indexId)),

  // Replace the working doc wholesale (PRD generation). It becomes the new diff base too — a freshly
  // generated schema is the starting point, not a delta from whatever was loaded before (spec §1).
  replaceDoc: async (doc) => {
    const prev = get().doc;
    set((s) => ({
      doc,
      past: [...s.past, ...(prev ? [prev] : [])].slice(-MAX_HISTORY),
      future: [],
      baseline: doc,
      savedSignature: edit.schemaSignature(doc),
      approved: null,
      drift: null,
      selectedTableId: null,
    }));
    await get().refresh();
  },

  // Compare the working design against a real database (database-connection milestone §3). Pure
  // surfacing of `/design/drift`: the engine does the three-way categorisation; the canvas only sends
  // the designed schema + a live DSN and stores the report. Never writes to the database (AD-5).
  compareWithDatabase: async (liveDsn) => {
    const { doc } = get();
    if (!doc) return { ok: false, error: "nothing to compare" };
    set({ driftBusy: true });
    try {
      const report = await driftAgainstDatabase(doc, liveDsn);
      set({ drift: report });
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    } finally {
      set({ driftBusy: false });
    }
  },
  clearDrift: () => set({ drift: null }),

  // The one entry point for drawing a relation — performed as a single atomic edit so it lands as one
  // undo step and one engine round-trip. If no FK field can be determined it is a NO-OP: the front-end
  // refuses to create an incomplete relation, and the engine resolves the new FK column's physical
  // type (a uuid PK ⇒ a uuid FK) on the very next render (spec §3 — the lesson of the whole project).
  connect: (args) =>
    get().mutate((d) => {
      let doc = d;
      let fkId = args.fkFieldId;
      if (!fkId && args.newFkFieldName?.trim()) {
        const created = edit.addField(doc, args.fromTableId, {
          name: args.newFkFieldName.trim(),
          semanticType: "foreign_key",
          nullable: false,
        });
        doc = created.doc;
        fkId = created.fieldId;
      }
      if (!fkId) return doc; // guard: never persist a relation without its foreign-key field
      return edit.addRelation(doc, {
        type: args.type,
        fromTableId: args.fromTableId,
        toTableId: args.toTableId,
        foreignKeyFieldId: fkId,
        onDelete: args.onDelete ?? null,
      }).doc;
    }),

  // Approve = drive the engine's existing gate (AD-5), never a front-end decision. A brownfield
  // session whose baseline IS the canvas base lets the gate compute the same migration the diff/risk
  // preview showed: apply the working doc → validate → submit → approve. The gate's guards (green
  // validation, unacknowledged-critical) come back as the result; the UI reflects them (spec §2/§3).
  runApprove: async (acknowledgeCritical) => {
    const { doc, baseline } = get();
    if (!doc) return { status: "error", message: "nothing to approve" };
    set({ approving: true });
    try {
      const sid = await createBaselineSession(baseline ?? doc);
      await applySessionSchema(sid, doc);
      const validated = await validateSession(sid);
      if (validated.state !== "validated") {
        const report = (validated.report ?? {}) as { summary?: Record<string, number> };
        return { status: "validation_error", summary: report.summary };
      }
      await submitSession(sid);
      const { status, body } = await approveSession(sid, { approvedBy: APPROVED_BY, acknowledgeCritical });
      if (status === 200) {
        // The approved schema becomes the new base — subsequent changes diff against it (spec §2).
        set({
          baseline: doc,
          savedSignature: edit.schemaSignature(doc),
          approved: { schemaVersion: body.schemaVersion as string, checksum: body.checksum as string },
        });
        await get().refresh(); // diff collapses to empty against the new base
        return { status: "approved", schemaVersion: body.schemaVersion as string, checksum: body.checksum as string };
      }
      if (body.reason === "critical_migration_risk") {
        return { status: "critical_risk", blocking: (body.blocking ?? []) as BlockingOp[] };
      }
      if (body.reason === "validation_error") return { status: "validation_error" };
      return { status: "error", message: String(body.reason ?? body.error ?? `approve failed (${status})`) };
    } catch (e) {
      return { status: "error", message: e instanceof Error ? e.message : String(e) };
    } finally {
      set({ approving: false });
    }
  },

  select: (tableId) => set({ selectedTableId: tableId }),
  hover: (tableId) => set({ hoveredTableId: tableId }),
  setSearch: (q) => set({ search: q }),
  toggleTheme: () => {
    const next: Theme = get().theme === "dark" ? "light" : "dark";
    applyTheme(next);
    set({ theme: next });
  },
}));

// ---- derived selectors (kept pure so components stay thin) -------------------------------------

/** The set of table ids that match the current search query (empty query → all match). */
export function matchingTableIds(model: RenderModel | null, search: string): Set<string> | null {
  if (!model || !search.trim()) return null;
  const q = search.trim().toLowerCase();
  const ids = new Set<string>();
  for (const t of model.tables) {
    if (t.name.toLowerCase().includes(q) || t.fields.some((f) => f.name.toLowerCase().includes(q))) {
      ids.add(t.id);
    }
  }
  return ids;
}

/** Highlight focus = hovered table (transient) or selected table (sticky). */
export function focusNeighbours(model: RenderModel | null, focusId: string | null) {
  if (!model || !focusId) return null;
  return neighboursOf(model, focusId);
}

export function tableById(model: RenderModel | null, id: string | null): RenderTable | null {
  if (!model || !id) return null;
  return model.tables.find((t) => t.id === id) ?? null;
}

// ---- validation / dirty selectors (Milestone 2) -----------------------------------------------

/** Group findings by the entity (table/field/relation id) they point at, for inline display (spec §6). */
export function findingsByEntity(report: ValidationReport | null): Map<string, ValidationFinding[]> {
  const map = new Map<string, ValidationFinding[]>();
  for (const f of report?.findings ?? []) {
    if (!f.entity_id) continue;
    const list = map.get(f.entity_id) ?? [];
    list.push(f);
    map.set(f.entity_id, list);
  }
  return map;
}

export const isErrorSeverity = (s: string) => s === "error" || s === "security";

/** Does the working document differ from the last saved/loaded schema? Layout moves don't count. */
export function isDirty(state: Pick<CanvasState, "doc" | "savedSignature">): boolean {
  return !!state.doc && edit.schemaSignature(state.doc) !== state.savedSignature;
}

// ---- diff selectors (Milestone 3) -------------------------------------------------------------

const OP_COLOR: Record<string, ChangeColor> = {
  add: "green",
  drop: "red",
  rename: "blue",
  change: "yellow",
  set: "yellow",
};

export interface DiffColors {
  tables: Map<string, ChangeColor>;
  fields: Map<string, ChangeColor>;
  relations: Map<string, ChangeColor>;
}

/** Map the engine's operation list onto the entities still on the canvas, so changed tables/fields/
 *  relations can be tinted with the standard diff colours (spec §1). Drops aren't tintable (the
 *  entity is gone from the working view) — they still appear in the diff list. */
export function diffColors(diff: DiffResult | null): DiffColors {
  const tables = new Map<string, ChangeColor>();
  const fields = new Map<string, ChangeColor>();
  const relations = new Map<string, ChangeColor>();
  for (const op of diff?.operations ?? []) {
    const color = OP_COLOR[op.op.split("_", 1)[0]] ?? "yellow";
    if (op.fieldId && op.tableId) {
      fields.set(op.fieldId, color);
      if (!tables.has(op.tableId)) tables.set(op.tableId, "yellow"); // the table changed (not added)
    } else if (op.tableId) {
      tables.set(op.tableId, color); // add/rename/meta at the table level (add wins, runs first)
    } else if (op.entityId) {
      relations.set(op.entityId, color);
    }
  }
  return { tables, fields, relations };
}

export function diffIsEmpty(diff: DiffResult | null): boolean {
  return !diff || diff.operations.length === 0;
}

// ---- drift selectors (database-connection milestone §3) ---------------------------------------
// Reflect a three-way drift report onto the canvas with the same colour language as the diff. Only
// entities that exist in the *design* can be tinted (the canvas shows the designed schema); db-only
// drift (manual_prod_change / code_ahead_of_design) appears in the panel but has nothing to tint —
// exactly like a diff "drop". The engine decided every category; this only maps names → ids.
const DRIFT_COLOR: Record<string, ChangeColor> = {
  design_ahead_of_code: "green", // designed, not yet in the database
  migration_not_applied: "green", // designed + migrated, not live
  migration_incomplete: "yellow", // present everywhere but the type differs
};

export function driftColors(drift: DriftReport | null, model: RenderModel | null): DiffColors {
  const tables = new Map<string, ChangeColor>();
  const fields = new Map<string, ChangeColor>();
  const relations = new Map<string, ChangeColor>();
  if (!drift || !model) return { tables, fields, relations };
  const tableByName = new Map(model.tables.map((t) => [t.name, t]));
  for (const d of drift.drift) {
    const color = DRIFT_COLOR[d.category];
    if (!color) continue; // db-only entity → not on the canvas (shown in the drift panel only)
    const dot = d.entity.indexOf(".");
    const tableName = dot === -1 ? d.entity : d.entity.slice(0, dot);
    const columnName = dot === -1 ? null : d.entity.slice(dot + 1);
    const table = tableByName.get(tableName);
    if (!table) continue;
    if (columnName) {
      const field = table.fields.find((f) => f.name === columnName);
      if (field) {
        fields.set(field.id, color);
        if (!tables.has(table.id)) tables.set(table.id, "yellow"); // the table has drift inside it
      }
    } else {
      tables.set(table.id, color);
    }
  }
  return { tables, fields, relations };
}

export function driftIsEmpty(drift: DriftReport | null): boolean {
  return !drift || drift.drift.length === 0;
}

export function severityCounts(report: ValidationReport | null): { errors: number; warnings: number } {
  const findings = report?.findings ?? [];
  let errors = 0;
  let warnings = 0;
  for (const f of findings) {
    if (isErrorSeverity(f.severity)) errors++;
    else warnings++;
  }
  return { errors, warnings };
}
