// The editable schema_json document (Milestone 2). The canvas now holds the *source* document and
// mutates it, but these helpers are deliberately **structural only** — they add/remove/rename
// records and never decide anything a database engine should decide. Type resolution (an FK's
// physical type), validity, completeness and migration risk all stay in the engine: every mutation
// is followed by a round-trip to `/design/render` (resolved view) and `/core/validate` (findings).
// That keeps the spec's hard rule intact — "the front-end is an editing tool, not a decision-maker"
// (spec §0) — and the Stable IDs (AD-1) of existing entities are never rewritten by an edit.

import type { RelationType } from "./types";

export interface SchemaField {
  id: string;
  name: string;
  semanticType: string;
  nullable: boolean;
  isPrimaryKey?: boolean;
  enumId?: string | null;
  comment?: string | null;
  [key: string]: unknown; // preserve unknown field props (overrides, default, …) through edits
}

export interface SchemaTable {
  id: string;
  name: string;
  comment?: string | null;
  domain?: string | null;
  kind?: string | null;
  fields: SchemaField[];
  [key: string]: unknown;
}

export interface SchemaRelation {
  id: string;
  name?: string | null;
  type: RelationType;
  fromTableId: string;
  toTableId?: string | null;
  foreignKeyFieldId?: string | null;
  onDelete?: string | null;
  onUpdate?: string | null;
  [key: string]: unknown;
}

export interface SchemaEnumValue {
  value: string;
  label?: Record<string, string> | null;
}

export interface SchemaEnum {
  id: string;
  name: string;
  values: SchemaEnumValue[];
  [key: string]: unknown;
}

export interface SchemaIndex {
  id: string;
  tableId: string;
  columns: string[]; // field ids
  unique: boolean;
  type?: string | null;
  [key: string]: unknown;
}

export interface PresentationNode {
  tableId: string;
  x: number;
  y: number;
  collapsed?: boolean;
  color?: string | null;
  group?: string | null;
}

export interface SchemaDoc {
  formatVersion: string;
  meta?: Record<string, unknown>;
  logical: {
    tables: SchemaTable[];
    relations?: SchemaRelation[];
    enums?: SchemaEnum[];
  };
  physical?: { indexes?: SchemaIndex[]; [key: string]: unknown };
  presentation?: { nodes: PresentationNode[]; [key: string]: unknown };
  [key: string]: unknown; // keep semantic/generation/extensions layers intact across edits
}

// ---- ids --------------------------------------------------------------------------------------
// New entities get fresh Stable IDs matching the format's pattern: `^(tbl|fld|rel|…)_[0-9A-Za-z._-]{4,}$`.
const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

export function genId(prefix: "tbl" | "fld" | "rel" | "enm" | "idx"): string {
  let body = "";
  for (let i = 0; i < 11; i++) {
    body += ID_ALPHABET[Math.floor(Math.random() * ID_ALPHABET.length)];
  }
  return `${prefix}_${body}`;
}

// ---- pure helpers ------------------------------------------------------------------------------
export function cloneDoc(doc: SchemaDoc): SchemaDoc {
  return structuredClone(doc);
}

function relations(doc: SchemaDoc): SchemaRelation[] {
  return doc.logical.relations ?? [];
}

export function tableById(doc: SchemaDoc, tableId: string): SchemaTable | undefined {
  return doc.logical.tables.find((t) => t.id === tableId);
}

// ---- mutations (each returns a NEW document; callers re-render + re-validate via the engine) ----

export function addTable(doc: SchemaDoc, name: string): { doc: SchemaDoc; tableId: string } {
  const next = cloneDoc(doc);
  const tableId = genId("tbl");
  // A new table ships with an `id` uuid primary key — a sane, engine-valid starting point the user
  // can then edit. The engine still has the final say on validity.
  next.logical.tables.push({
    id: tableId,
    name,
    kind: "normal",
    fields: [{ id: genId("fld"), name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }],
  });
  return { doc: next, tableId };
}

export function removeTable(doc: SchemaDoc, tableId: string): SchemaDoc {
  const next = cloneDoc(doc);
  next.logical.tables = next.logical.tables.filter((t) => t.id !== tableId);
  // Relations touching the table become dangling — drop them so we never leave broken references.
  next.logical.relations = relations(next).filter(
    (r) => r.fromTableId !== tableId && r.toTableId !== tableId,
  );
  if (next.physical?.indexes) {
    next.physical.indexes = next.physical.indexes.filter((i) => i.tableId !== tableId);
  }
  if (next.presentation) {
    next.presentation.nodes = next.presentation.nodes.filter((n) => n.tableId !== tableId);
  }
  return next;
}

export function updateTable(
  doc: SchemaDoc,
  tableId: string,
  patch: Partial<Pick<SchemaTable, "name" | "domain" | "kind" | "comment">>,
): SchemaDoc {
  const next = cloneDoc(doc);
  const table = tableById(next, tableId);
  if (table) Object.assign(table, patch);
  return next;
}

export function addField(
  doc: SchemaDoc,
  tableId: string,
  field: { name: string; semanticType: string; nullable?: boolean; isPrimaryKey?: boolean },
): { doc: SchemaDoc; fieldId: string } {
  const next = cloneDoc(doc);
  const table = tableById(next, tableId);
  const fieldId = genId("fld");
  if (table) {
    table.fields.push({
      id: fieldId,
      name: field.name,
      semanticType: field.semanticType,
      nullable: field.nullable ?? true,
      isPrimaryKey: field.isPrimaryKey ?? false,
    });
  }
  return { doc: next, fieldId };
}

export function updateField(
  doc: SchemaDoc,
  tableId: string,
  fieldId: string,
  patch: Partial<Pick<SchemaField, "name" | "semanticType" | "nullable" | "isPrimaryKey" | "enumId">>,
): SchemaDoc {
  const next = cloneDoc(doc);
  const field = tableById(next, tableId)?.fields.find((f) => f.id === fieldId);
  if (field) Object.assign(field, patch);
  return next;
}

// ---- privacy override (intelligence milestone §3 — confirming a sensitive-field suggestion) -------
// Marking a field sensitive is a structural edit like any other: it sets a `privacy` override the
// engine's Type System reads (pii + sensitivity drive masking). It flows through validate/diff so it
// behaves exactly like a manual change — the intelligence proposes, the engine applies (spec §5).
export function markFieldSensitive(
  doc: SchemaDoc,
  tableId: string,
  fieldId: string,
  sensitivity = "medium",
): SchemaDoc {
  const next = cloneDoc(doc);
  const field = tableById(next, tableId)?.fields.find((f) => f.id === fieldId);
  if (field) {
    const overrides = (field.overrides && typeof field.overrides === "object" ? { ...field.overrides } : {}) as Record<
      string,
      unknown
    >;
    const privacy = (overrides.privacy && typeof overrides.privacy === "object" ? { ...overrides.privacy } : {}) as Record<
      string,
      unknown
    >;
    privacy.pii = true;
    if (!privacy.sensitivity || privacy.sensitivity === "none") privacy.sensitivity = sensitivity;
    overrides.privacy = privacy;
    field.overrides = overrides;
  }
  return next;
}

// ---- duplicate table (spec phase 2 §2 C6 — structural, fresh Stable IDs) ------------------------
// A deep copy with BRAND-NEW ids (AD-1: ids are never copied, always minted) and a `_copy` name. The
// copy's relations are NOT carried over (they reference the originals' field ids); the user re-draws
// them so every FK stays well-formed. Validated like any other edit on the next engine round-trip.
export function duplicateTable(doc: SchemaDoc, tableId: string): { doc: SchemaDoc; tableId: string } {
  const next = cloneDoc(doc);
  const source = tableById(next, tableId);
  if (!source) return { doc: next, tableId };
  const newId = genId("tbl");
  const existing = new Set(next.logical.tables.map((t) => t.name));
  let name = `${source.name}_copy`;
  let n = 2;
  while (existing.has(name)) name = `${source.name}_copy${n++}`;
  const copy: SchemaTable = {
    ...structuredClone(source),
    id: newId,
    name,
    fields: source.fields.map((f) => ({ ...structuredClone(f), id: genId("fld") })),
  };
  next.logical.tables.push(copy);
  // Offset the copy on the canvas so it doesn't land exactly on the original.
  if (next.presentation) {
    const src = next.presentation.nodes.find((p) => p.tableId === tableId);
    if (src) next.presentation.nodes.push({ ...src, tableId: newId, x: src.x + 48, y: src.y + 48 });
  }
  return { doc: next, tableId: newId };
}

// ---- timestamps / soft-delete (spec phase 2 §2 C7 — REAL columns, not magic flags) --------------
// These expand to ordinary datetime columns so they flow through validate + diff + migration like any
// other field. The toggle is purely a convenience for adding/removing the trio.
export const TIMESTAMP_FIELDS = ["created_at", "updated_at"] as const;
export const SOFT_DELETE_FIELD = "deleted_at";

function hasField(table: SchemaTable, name: string): boolean {
  return table.fields.some((f) => f.name === name);
}

export function tableHasTimestamps(table: SchemaTable): boolean {
  return TIMESTAMP_FIELDS.every((n) => hasField(table, n));
}

export function tableHasSoftDelete(table: SchemaTable): boolean {
  return hasField(table, SOFT_DELETE_FIELD);
}

function addNamedColumns(table: SchemaTable, names: readonly string[], nullable: boolean): void {
  for (const name of names) {
    if (!hasField(table, name)) {
      table.fields.push({ id: genId("fld"), name, semanticType: "datetime", nullable });
    }
  }
}

function removeNamedColumns(table: SchemaTable, names: readonly string[]): void {
  table.fields = table.fields.filter((f) => !names.includes(f.name));
}

export function setTimestamps(doc: SchemaDoc, tableId: string, on: boolean): SchemaDoc {
  const next = cloneDoc(doc);
  const table = tableById(next, tableId);
  if (table) {
    if (on) addNamedColumns(table, TIMESTAMP_FIELDS, false);
    else removeNamedColumns(table, TIMESTAMP_FIELDS);
  }
  return next;
}

export function setSoftDelete(doc: SchemaDoc, tableId: string, on: boolean): SchemaDoc {
  const next = cloneDoc(doc);
  const table = tableById(next, tableId);
  if (table) {
    if (on) addNamedColumns(table, [SOFT_DELETE_FIELD], true);
    else removeNamedColumns(table, [SOFT_DELETE_FIELD]);
  }
  return next;
}

// ---- reusable enums (spec phase 2 §B — engine `logical.enums`) ----------------------------------
function enums(doc: SchemaDoc): SchemaEnum[] {
  return doc.logical.enums ?? [];
}

export function addEnum(doc: SchemaDoc, name: string, values: string[] = []): { doc: SchemaDoc; enumId: string } {
  const next = cloneDoc(doc);
  const enumId = genId("enm");
  if (!next.logical.enums) next.logical.enums = [];
  next.logical.enums.push({ id: enumId, name, values: values.map((v) => ({ value: v })) });
  return { doc: next, enumId };
}

export function updateEnum(
  doc: SchemaDoc,
  enumId: string,
  patch: { name?: string; values?: string[] },
): SchemaDoc {
  const next = cloneDoc(doc);
  const e = enums(next).find((x) => x.id === enumId);
  if (e) {
    if (patch.name !== undefined) e.name = patch.name;
    if (patch.values !== undefined) e.values = patch.values.map((v) => ({ value: v }));
  }
  return next;
}

export function removeEnum(doc: SchemaDoc, enumId: string): SchemaDoc {
  const next = cloneDoc(doc);
  next.logical.enums = enums(next).filter((e) => e.id !== enumId);
  // Detach the enum from any field still pointing at it (the engine would flag a dangling ref).
  for (const t of next.logical.tables) {
    for (const f of t.fields) if (f.enumId === enumId) f.enumId = null;
  }
  return next;
}

// ---- explicit indexes (spec phase 2 §B — engine `physical.indexes`) -----------------------------
function indexes(doc: SchemaDoc): SchemaIndex[] {
  return doc.physical?.indexes ?? [];
}

export function addIndex(
  doc: SchemaDoc,
  tableId: string,
  index: { columns: string[]; unique?: boolean; type?: string },
): { doc: SchemaDoc; indexId: string } {
  const next = cloneDoc(doc);
  const indexId = genId("idx");
  if (!next.physical) next.physical = {};
  if (!next.physical.indexes) next.physical.indexes = [];
  next.physical.indexes.push({
    id: indexId,
    tableId,
    columns: index.columns,
    unique: index.unique ?? false,
    type: index.type ?? "btree",
  });
  return { doc: next, indexId };
}

export function removeIndex(doc: SchemaDoc, indexId: string): SchemaDoc {
  const next = cloneDoc(doc);
  if (next.physical?.indexes) {
    next.physical = { ...next.physical, indexes: indexes(next).filter((i) => i.id !== indexId) };
  }
  return next;
}

export function indexesForTable(doc: SchemaDoc, tableId: string): SchemaIndex[] {
  return indexes(doc).filter((i) => i.tableId === tableId);
}

export function removeField(doc: SchemaDoc, tableId: string, fieldId: string): SchemaDoc {
  const next = cloneDoc(doc);
  const table = tableById(next, tableId);
  if (table) table.fields = table.fields.filter((f) => f.id !== fieldId);
  // A relation that used this column as its FK is now incomplete → drop it (the engine would flag
  // it anyway; removing keeps the canvas from ever holding a known-broken relation, spec §3).
  next.logical.relations = relations(next).filter((r) => r.foreignKeyFieldId !== fieldId);
  // Likewise drop any index column referencing the removed field (and empty indexes).
  if (next.physical?.indexes) {
    next.physical.indexes = next.physical.indexes
      .map((i) => ({ ...i, columns: i.columns.filter((c) => c !== fieldId) }))
      .filter((i) => i.columns.length > 0);
  }
  return next;
}

export interface NewRelation {
  type: RelationType;
  fromTableId: string;
  toTableId: string;
  foreignKeyFieldId: string; // REQUIRED — a relation can never be created without its FK field (spec §2/§3)
  onDelete?: string | null;
  name?: string | null;
}

export function addRelation(doc: SchemaDoc, rel: NewRelation): { doc: SchemaDoc; relationId: string } {
  const next = cloneDoc(doc);
  const relationId = genId("rel");
  if (!next.logical.relations) next.logical.relations = [];
  next.logical.relations.push({
    id: relationId,
    type: rel.type,
    fromTableId: rel.fromTableId,
    toTableId: rel.toTableId,
    foreignKeyFieldId: rel.foreignKeyFieldId,
    onDelete: rel.onDelete ?? null,
    name: rel.name ?? null,
  });
  return { doc: next, relationId };
}

export function removeRelation(doc: SchemaDoc, relationId: string): SchemaDoc {
  const next = cloneDoc(doc);
  next.logical.relations = relations(next).filter((r) => r.id !== relationId);
  return next;
}

// ---- presentation (layout is NOT a schema change, spec §4) -------------------------------------
export function setPositions(doc: SchemaDoc, positions: Record<string, { x: number; y: number }>): SchemaDoc {
  const next = cloneDoc(doc);
  const existing = new Map((next.presentation?.nodes ?? []).map((n) => [n.tableId, n]));
  const nodes: PresentationNode[] = next.logical.tables.map((t) => {
    const prev = existing.get(t.id);
    const pos = positions[t.id] ?? (prev ? { x: prev.x, y: prev.y } : { x: 0, y: 0 });
    return { ...(prev ?? {}), tableId: t.id, x: pos.x, y: pos.y };
  });
  next.presentation = { ...(next.presentation ?? {}), nodes };
  return next;
}

export function presentationNodes(doc: SchemaDoc): PresentationNode[] {
  return doc.presentation?.nodes ?? [];
}

/**
 * A stable signature of the *schema-affecting* content only — presentation/layout is stripped so a
 * table move never registers as an unsaved schema change (spec §4/§5). This is what the "unsaved
 * changes" indicator and the future Milestone-3 diff are built on.
 */
export function schemaSignature(doc: SchemaDoc): string {
  const { presentation: _presentation, ...rest } = doc;
  return JSON.stringify(rest);
}
