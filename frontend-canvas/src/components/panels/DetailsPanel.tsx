import { useEffect, useState } from "react";
import { AlertCircle, Copy, KeyRound, Link2, Lock, Plus, Trash2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { findingsByEntity, isErrorSeverity, tableById, useCanvasStore } from "@/store/canvasStore";
import * as edit from "@/lib/schema";
import { CARDINALITY, FALLBACK_SEMANTIC_TYPES, type RenderEnum, type RenderField, type RenderModel, type RenderRelation, type ValidationFinding } from "@/lib/types";

const ENUM_TYPES = new Set(["enum", "status"]);

function relationsFor(model: RenderModel, tableId: string): { rel: RenderRelation; dir: "out" | "in" }[] {
  const out = model.relations
    .filter((r) => r.fromTableId === tableId)
    .map((r) => ({ rel: r, dir: "out" as const }));
  const inc = model.relations
    .filter((r) => r.toTableId === tableId && r.fromTableId !== tableId)
    .map((r) => ({ rel: r, dir: "in" as const }));
  return [...out, ...inc];
}

/** Inline findings for one entity (spec §6 — soft, guiding messages next to the field, not an alert). */
function Findings({ items }: { items: ValidationFinding[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <ul className="mt-1 space-y-0.5">
      {items.map((f, i) => (
        <li
          key={`${f.rule_id}-${i}`}
          className={cn(
            "flex items-start gap-1 text-2xs",
            isErrorSeverity(f.severity) ? "text-destructive" : "text-muted-foreground",
          )}
        >
          <AlertCircle className="mt-px h-3 w-3 shrink-0" />
          <span>
            {f.message}
            {f.fix ? ` — ${f.fix}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

function FieldEditor({
  field,
  tableId,
  typeOptions,
  findings,
  enums,
}: {
  field: RenderField;
  tableId: string;
  typeOptions: string[];
  findings: ValidationFinding[] | undefined;
  enums: RenderEnum[];
}) {
  const updateField = useCanvasStore((s) => s.updateField);
  const removeField = useCanvasStore((s) => s.removeField);
  const [name, setName] = useState(field.name);
  useEffect(() => setName(field.name), [field.name]);

  const commitName = () => {
    const trimmed = name.trim();
    if (trimmed && trimmed !== field.name) updateField(tableId, field.id, { name: trimmed });
    else setName(field.name);
  };

  // The dropdown always offers the chosen type even if it's not in the catalogue (e.g. unregistered).
  const options = typeOptions.includes(field.semanticType)
    ? typeOptions
    : [field.semanticType, ...typeOptions];

  return (
    <li className="rounded-md border border-border/70 px-2 py-1.5" data-testid={`field-editor-${field.name}`}>
      <div className="flex items-center gap-1.5">
        <span className="flex w-4 shrink-0 justify-center">
          {field.isPrimaryKey ? (
            <KeyRound className="h-3 w-3 text-pk" aria-label="primary key" />
          ) : field.isForeignKey ? (
            <Link2 className="h-3 w-3 text-fk" aria-label="foreign key" />
          ) : null}
        </span>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          aria-label={`Field name for ${field.name}`}
          className="h-7 flex-1 text-xs"
        />
        {field.pii && <Lock className="h-3 w-3 text-muted-foreground" aria-label="sensitive" />}
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={() => removeField(tableId, field.id)}
          aria-label={`Delete field ${field.name}`}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="mt-1 flex items-center gap-2 pl-5">
        <Select
          value={field.semanticType}
          onChange={(e) => updateField(tableId, field.id, { semanticType: e.target.value })}
          aria-label={`Type for ${field.name}`}
          className="h-7 flex-1"
          disabled={field.isForeignKey}
          title={field.isForeignKey ? "Foreign-key type is resolved from the referenced primary key" : undefined}
        >
          {options.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
        <span className="font-mono text-2xs text-muted-foreground" title="Resolved physical type">
          {field.physicalType}
        </span>
      </div>

      {ENUM_TYPES.has(field.semanticType) && (
        <div className="mt-1 flex items-center gap-2 pl-5">
          <Select
            value={field.enumId ?? ""}
            onChange={(e) => updateField(tableId, field.id, { enumId: e.target.value || null })}
            aria-label={`Enum for ${field.name}`}
            className="h-7 flex-1"
          >
            <option value="">— no enum —</option>
            {enums.map((en) => (
              <option key={en.id} value={en.id}>
                {en.name} ({en.values.length})
              </option>
            ))}
          </Select>
        </div>
      )}

      <div className="mt-1 flex items-center gap-3 pl-5 text-2xs">
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={field.isPrimaryKey}
            onChange={(e) => updateField(tableId, field.id, { isPrimaryKey: e.target.checked })}
          />
          PK
        </label>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={field.nullable}
            onChange={(e) => updateField(tableId, field.id, { nullable: e.target.checked })}
          />
          nullable
        </label>
      </div>

      <Findings items={findings} />
    </li>
  );
}

export function DetailsPanel() {
  const model = useCanvasStore((s) => s.model);
  const doc = useCanvasStore((s) => s.doc);
  const selectedId = useCanvasStore((s) => s.selectedTableId);
  const select = useCanvasStore((s) => s.select);
  const editable = useCanvasStore((s) => s.editable);
  const validation = useCanvasStore((s) => s.validation);
  const types = useCanvasStore((s) => s.types);
  const updateTable = useCanvasStore((s) => s.updateTable);
  const removeTable = useCanvasStore((s) => s.removeTable);
  const duplicateTable = useCanvasStore((s) => s.duplicateTable);
  const setTimestamps = useCanvasStore((s) => s.setTimestamps);
  const setSoftDelete = useCanvasStore((s) => s.setSoftDelete);
  const addField = useCanvasStore((s) => s.addField);
  const removeRelation = useCanvasStore((s) => s.removeRelation);

  const table = tableById(model, selectedId);
  const [tableName, setTableName] = useState(table?.name ?? "");
  const [comment, setComment] = useState(table?.comment ?? "");
  useEffect(() => setTableName(table?.name ?? ""), [table?.id, table?.name]);
  useEffect(() => setComment(table?.comment ?? ""), [table?.id, table?.comment]);

  if (!model || !table) return null;

  const docTable = doc?.logical.tables.find((t) => t.id === table.id);
  const fieldNames = new Set(table.fields.map((f) => f.name));
  const hasTimestamps = ["created_at", "updated_at"].every((n) => fieldNames.has(n));
  const hasSoftDelete = fieldNames.has("deleted_at");

  const findingMap = findingsByEntity(validation);
  const typeOptions = (types.length ? types.map((t) => t.id) : FALLBACK_SEMANTIC_TYPES).slice().sort();
  const nameById = (id: string | null) => (id ? model.tables.find((t) => t.id === id)?.name ?? id : "—");

  const commitTableName = () => {
    const trimmed = tableName.trim();
    if (trimmed && trimmed !== table.name) updateTable(table.id, { name: trimmed });
    else setTableName(table.name);
  };
  const commitComment = () => {
    const trimmed = comment.trim();
    if (trimmed !== (table.comment ?? "")) updateTable(table.id, { comment: trimmed || null });
  };

  return (
    <aside className="flex h-full w-80 shrink-0 flex-col border-l border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          {editable ? (
            <Input
              value={tableName}
              onChange={(e) => setTableName(e.target.value)}
              onBlur={commitTableName}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
              aria-label="Table name"
              className="h-7 text-sm font-semibold"
            />
          ) : (
            <div className="truncate text-sm font-semibold">{table.name}</div>
          )}
          {editable ? (
            <Input
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              onBlur={commitComment}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
              placeholder="Add a description…"
              aria-label="Table description"
              className="mt-1 h-6 text-2xs"
            />
          ) : (
            table.comment && <div className="mt-0.5 truncate text-2xs text-muted-foreground">{table.comment}</div>
          )}
        </div>
        {table.kind && table.kind !== "normal" && <Badge>{table.kind}</Badge>}
        <Button variant="ghost" size="icon" onClick={() => select(null)} aria-label="Close details">
          <X className="h-4 w-4" />
        </Button>
      </div>

      <Findings items={findingMap.get(table.id)} />

      <div className="flex-1 overflow-y-auto">
        <section className="px-4 py-3">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
              Fields ({table.fields.length})
            </h3>
            {editable && (
              <Button
                variant="outline"
                size="sm"
                className="h-6"
                onClick={() => addField(table.id, { name: "new_field", semanticType: "string" })}
                aria-label="Add field"
              >
                <Plus className="h-3 w-3" /> Field
              </Button>
            )}
          </div>

          {editable ? (
            <ul className="space-y-1.5">
              {table.fields.map((f) => (
                <FieldEditor
                  key={f.id}
                  field={f}
                  tableId={table.id}
                  typeOptions={typeOptions}
                  findings={findingMap.get(f.id)}
                  enums={model.enums}
                />
              ))}
            </ul>
          ) : (
            <ul className="space-y-1">
              {table.fields.map((f) => (
                <li key={f.id} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs">
                  <span className={cn("flex-1 truncate", f.isPrimaryKey && "font-semibold")}>{f.name}</span>
                  <span className="font-mono text-2xs text-muted-foreground">{f.physicalType}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="border-t border-border px-4 py-3">
          <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
            Relations
          </h3>
          {relationsFor(model, table.id).length === 0 ? (
            <p className="text-2xs text-muted-foreground">No relations. Drag from one table to another to add one.</p>
          ) : (
            <ul className="space-y-1.5">
              {relationsFor(model, table.id).map(({ rel, dir }) => (
                <li key={rel.id} className="rounded-md bg-muted/50 px-2 py-1.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate">
                      {dir === "out" ? "→ " : "← "}
                      {nameById(dir === "out" ? rel.toTableId : rel.fromTableId)}
                    </span>
                    <div className="flex items-center gap-1">
                      <Badge>{CARDINALITY[rel.type] ?? rel.type}</Badge>
                      {editable && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() => removeRelation(rel.id)}
                          aria-label="Delete relation"
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      )}
                    </div>
                  </div>
                  {rel.onDelete && (
                    <div className="mt-0.5 text-2xs text-muted-foreground">on delete: {rel.onDelete}</div>
                  )}
                  <Findings items={findingMap.get(rel.id)} />
                </li>
              ))}
            </ul>
          )}
        </section>

        {editable && docTable && <IndexesSection docTable={docTable} model={model} />}

        {editable && (
          <section className="border-t border-border px-4 py-3">
            <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">Options</h3>
            <label className="mb-2 flex flex-col gap-1 text-2xs text-muted-foreground">
              Domain / group
              <Input
                defaultValue={docTable?.domain ?? ""}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  if (v !== (docTable?.domain ?? "")) updateTable(table.id, { domain: v || null });
                }}
                placeholder="e.g. billing"
                aria-label="Table domain"
                className="h-7 text-xs"
              />
            </label>
            <label className="flex items-center justify-between py-0.5 text-2xs">
              <span>Timestamps (created_at, updated_at)</span>
              <input
                type="checkbox"
                checked={hasTimestamps}
                onChange={(e) => setTimestamps(table.id, e.target.checked)}
                aria-label="Toggle timestamp columns"
              />
            </label>
            <label className="flex items-center justify-between py-0.5 text-2xs">
              <span>Soft delete (deleted_at)</span>
              <input
                type="checkbox"
                checked={hasSoftDelete}
                onChange={(e) => setSoftDelete(table.id, e.target.checked)}
                aria-label="Toggle soft-delete column"
              />
            </label>
            <p className="mt-1 text-2xs text-muted-foreground">These add real datetime columns, validated and diffed like any field.</p>
          </section>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-border px-4 py-2">
        <span className="text-2xs text-muted-foreground">
          {editable ? "Editing · changes are validated by the engine" : "Read-only"}
        </span>
        {editable && (
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={() => duplicateTable(table.id)}
              aria-label="Duplicate table"
            >
              <Copy className="h-3.5 w-3.5" /> Duplicate
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-destructive"
              onClick={() => removeTable(table.id)}
              aria-label="Delete table"
            >
              <Trash2 className="h-3.5 w-3.5" /> Delete table
            </Button>
          </div>
        )}
      </div>
    </aside>
  );
}

/** Explicit (possibly composite) indexes on the engine's `physical.indexes` (unify spec phase 2 §B). */
function IndexesSection({ docTable, model }: { docTable: edit.SchemaTable; model: RenderModel }) {
  const doc = useCanvasStore((s) => s.doc);
  const addIndex = useCanvasStore((s) => s.addIndex);
  const removeIndex = useCanvasStore((s) => s.removeIndex);
  const indexes = doc ? edit.indexesForTable(doc, docTable.id) : [];
  const fieldName = (fid: string) => docTable.fields.find((f) => f.id === fid)?.name ?? fid;
  const modelTable = model.tables.find((t) => t.id === docTable.id);

  const [columnId, setColumnId] = useState(docTable.fields[0]?.id ?? "");
  const [unique, setUnique] = useState(false);
  useEffect(() => setColumnId(docTable.fields[0]?.id ?? ""), [docTable.id]);

  const add = () => {
    if (columnId) addIndex(docTable.id, { columns: [columnId], unique });
  };

  return (
    <section className="border-t border-border px-4 py-3">
      <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
        Indexes ({indexes.length})
      </h3>
      {indexes.length > 0 && (
        <ul className="mb-2 space-y-1">
          {indexes.map((idx) => (
            <li key={idx.id} className="flex items-center justify-between rounded-md bg-muted/50 px-2 py-1 text-2xs">
              <span className="truncate">
                {idx.columns.map(fieldName).join(", ")}
                {idx.unique && <span className="ml-1 text-pk">· unique</span>}
              </span>
              <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() => removeIndex(idx.id)} aria-label="Remove index">
                <Trash2 className="h-3 w-3" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-1.5">
        <Select value={columnId} onChange={(e) => setColumnId(e.target.value)} aria-label="Index column" className="h-7 flex-1">
          {(modelTable?.fields ?? []).map((f) => (
            <option key={f.id} value={f.id}>
              {f.name}
            </option>
          ))}
        </Select>
        <label className="flex items-center gap-1 text-2xs">
          <input type="checkbox" checked={unique} onChange={(e) => setUnique(e.target.checked)} aria-label="Unique index" /> uniq
        </label>
        <Button variant="outline" size="sm" className="h-7" onClick={add} aria-label="Add index">
          <Plus className="h-3 w-3" />
        </Button>
      </div>
    </section>
  );
}
