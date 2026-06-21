import { memo, useEffect, useRef, useState } from "react";
import { Handle, Position, useStore, type NodeProps } from "reactflow";
import { AlertCircle, KeyRound, Link2, Lock, Table2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { CHANGE_NODE, CHANGE_ROW } from "@/lib/diffStyle";
import { Tooltip } from "@/components/ui/tooltip";
import { useCanvasStore } from "@/store/canvasStore";
import type { TableNodeData } from "@/lib/graph";
import type { ChangeColor, RenderField } from "@/lib/types";

// Below this zoom we render the table name only (semantic zoom, spec §4): the map stays readable
// when there are many tables; zooming in reveals the fields.
const FIELD_ZOOM_THRESHOLD = 0.5;

/** A click-to-commit inline editor (double-click a name on the canvas to rename it, spec §2). */
function InlineEdit({
  value,
  onCommit,
  onCancel,
  className,
}: {
  value: string;
  onCommit: (next: string) => void;
  onCancel: () => void;
  className?: string;
}) {
  const [draft, setDraft] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => ref.current?.select(), []);
  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== value) onCommit(trimmed);
    else onCancel();
  };
  return (
    <input
      ref={ref}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        else if (e.key === "Escape") onCancel();
      }}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      className={cn(
        "nodrag w-full rounded border border-primary bg-background px-1 text-xs outline-none",
        className,
      )}
    />
  );
}

function FieldRow({
  field,
  tableId,
  editable,
  errored,
  change,
}: {
  field: RenderField;
  tableId: string;
  editable: boolean;
  errored: boolean;
  change: ChangeColor | undefined;
}) {
  const [editing, setEditing] = useState(false);
  const updateField = useCanvasStore((s) => s.updateField);

  return (
    <div
      className={cn(
        "flex items-center gap-1.5 px-3 py-1 text-xs border-t border-border/60 transition-colors",
        change && CHANGE_ROW[change],
        errored && "bg-destructive/10",
      )}
      data-testid={`field-${field.name}`}
    >
      <span className="flex w-4 shrink-0 justify-center">
        {field.isPrimaryKey ? (
          <Tooltip label="Primary key" side="top">
            <KeyRound className="h-3 w-3 text-pk" aria-label="primary key" />
          </Tooltip>
        ) : field.isForeignKey ? (
          <Tooltip label="Foreign key" side="top">
            <Link2 className="h-3 w-3 text-fk" aria-label="foreign key" />
          </Tooltip>
        ) : null}
      </span>

      {editing ? (
        <InlineEdit
          value={field.name}
          onCommit={(name) => {
            updateField(tableId, field.id, { name });
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
          className="flex-1"
        />
      ) : (
        <span
          className={cn("flex-1 truncate", field.isPrimaryKey && "font-semibold", editable && "cursor-text")}
          onDoubleClick={editable ? () => setEditing(true) : undefined}
          title={editable ? "Double-click to rename" : undefined}
        >
          {field.name}
        </span>
      )}

      {errored && (
        <Tooltip label="This field has a validation error" side="top">
          <AlertCircle className="h-3 w-3 text-destructive" aria-label="field has an error" />
        </Tooltip>
      )}
      {field.pii && (
        <Tooltip label={`Sensitive${field.sensitivity ? ` · ${field.sensitivity}` : ""}`} side="top">
          <Lock className="h-3 w-3 text-muted-foreground" aria-label="sensitive field" />
        </Tooltip>
      )}
      <span className="ml-1 shrink-0 font-mono text-2xs text-muted-foreground" data-testid="field-type">
        {field.physicalType}
        {field.nullable ? "?" : ""}
      </span>
    </div>
  );
}

function TableNodeImpl({ data, selected }: NodeProps<TableNodeData>) {
  const zoom = useStore((s) => s.transform[2]);
  const { table, highlighted, dimmed, editable, hasError, errorFieldIds, changeColor, fieldChanges } = data;
  const showFields = zoom >= FIELD_ZOOM_THRESHOLD;
  const [editingName, setEditingName] = useState(false);
  const updateTable = useCanvasStore((s) => s.updateTable);

  return (
    <div
      className={cn(
        "w-[252px] overflow-hidden rounded-lg border bg-card text-card-foreground shadow-sm",
        "transition-[box-shadow,border-color,transform] duration-200 ease-smooth",
        (highlighted || selected) && "border-primary shadow-lg ring-1 ring-primary/40",
        changeColor && CHANGE_NODE[changeColor],
        hasError && "border-destructive/70",
        dimmed && "opacity-30",
      )}
      data-testid={`table-node-${table.name}`}
    >
      {/* Handles are connectable only while editing — dragging one to another table draws a relation
          (spec §2). In read-only mode they are present but inert (Milestone 1 behaviour). */}
      <Handle type="target" position={Position.Left} isConnectable={editable} />
      <Handle type="source" position={Position.Right} isConnectable={editable} />

      <div className="flex items-center gap-2 bg-muted/60 px-3 py-2">
        <Table2 className="h-3.5 w-3.5 shrink-0 text-primary" />
        {editingName ? (
          <InlineEdit
            value={table.name}
            onCommit={(name) => {
              updateTable(table.id, { name });
              setEditingName(false);
            }}
            onCancel={() => setEditingName(false)}
            className="flex-1 text-sm font-semibold"
          />
        ) : (
          <span
            className={cn("flex-1 truncate text-sm font-semibold", editable && "cursor-text")}
            onDoubleClick={editable ? () => setEditingName(true) : undefined}
            title={editable ? "Double-click to rename" : undefined}
          >
            {table.name}
          </span>
        )}
        {hasError && <AlertCircle className="h-3.5 w-3.5 shrink-0 text-destructive" aria-label="table has errors" />}
        {table.kind && table.kind !== "normal" && (
          <span className="rounded bg-accent px-1 text-2xs text-accent-foreground">{table.kind}</span>
        )}
      </div>

      {showFields ? (
        <div>
          {table.fields.map((f) => (
            <FieldRow
              key={f.id}
              field={f}
              tableId={table.id}
              editable={editable}
              errored={errorFieldIds.has(f.id)}
              change={fieldChanges.get(f.id)}
            />
          ))}
        </div>
      ) : (
        <div className="px-3 py-1.5 text-2xs text-muted-foreground">
          {table.fields.length} field{table.fields.length === 1 ? "" : "s"}
        </div>
      )}
    </div>
  );
}

export const TableNode = memo(TableNodeImpl);
