import { useEffect, useState } from "react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { tableById, useCanvasStore } from "@/store/canvasStore";
import { CARDINALITY, type RelationType } from "@/lib/types";

// Drawing a relation forces the two decisions that prevent the recurring "incomplete relation" bug
// (spec §2/§3): the cardinality, and WHICH foreign-key column carries it. Confirm stays disabled
// until an FK field is chosen, so the canvas can never emit a relation without `foreignKeyFieldId`.
// The FK column's type is NOT decided here — the engine resolves it (a uuid PK ⇒ a uuid FK).

const RELATION_TYPES: RelationType[] = ["one_to_many", "one_to_one", "many_to_one"];

export interface PendingConnection {
  source: string; // fromTableId — the table that will hold the FK
  target: string; // toTableId — the referenced table
}

export function RelationDialog({
  pending,
  onClose,
}: {
  pending: PendingConnection | null;
  onClose: () => void;
}) {
  const model = useCanvasStore((s) => s.model);
  const connect = useCanvasStore((s) => s.connect);

  const from = tableById(model, pending?.source ?? null);
  const to = tableById(model, pending?.target ?? null);

  const [type, setType] = useState<RelationType>("one_to_many");
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [fkFieldId, setFkFieldId] = useState<string>("");
  const [newName, setNewName] = useState<string>("");

  // Reset the form whenever a new connection is started.
  const key = `${pending?.source}->${pending?.target}`;
  useEffect(() => {
    setType("one_to_many");
    setMode("new");
    setFkFieldId(""); // existing-field mode requires an explicit pick (no accidental FK on the PK)
    setNewName(to ? `${to.name}_id` : "");
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!pending || !from || !to) return null;

  const canConfirm = mode === "existing" ? !!fkFieldId : newName.trim().length > 0;

  const confirm = () => {
    if (!canConfirm) return;
    connect({
      fromTableId: from.id,
      toTableId: to.id,
      type,
      fkFieldId: mode === "existing" ? fkFieldId : undefined,
      newFkFieldName: mode === "new" ? newName : undefined,
      onDelete: "cascade",
    });
    onClose();
  };

  return (
    <Dialog open onClose={onClose} title="New relation">
      <div className="space-y-3 text-xs">
        <p className="text-muted-foreground">
          <span className="font-medium text-foreground">{from.name}</span> references{" "}
          <span className="font-medium text-foreground">{to.name}</span>. Choose the foreign-key column
          that carries it — its type is resolved from <span className="font-medium">{to.name}</span>’s
          primary key by the engine.
        </p>

        <label className="block">
          <span className="mb-1 block font-medium">Cardinality</span>
          <Select value={type} onChange={(e) => setType(e.target.value as RelationType)}>
            {RELATION_TYPES.map((t) => (
              <option key={t} value={t}>
                {t} ({CARDINALITY[t]})
              </option>
            ))}
          </Select>
        </label>

        <div>
          <span className="mb-1 block font-medium">Foreign-key field</span>
          <div className="mb-2 flex gap-1.5">
            <Button
              type="button"
              variant={mode === "new" ? "default" : "outline"}
              size="sm"
              onClick={() => setMode("new")}
            >
              New field
            </Button>
            <Button
              type="button"
              variant={mode === "existing" ? "default" : "outline"}
              size="sm"
              onClick={() => setMode("existing")}
              disabled={from.fields.length === 0}
            >
              Existing field
            </Button>
          </div>

          {mode === "new" ? (
            <Input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="foreign-key column name"
              aria-label="New foreign-key field name"
              className="h-8 text-xs"
            />
          ) : (
            <Select
              value={fkFieldId}
              onChange={(e) => setFkFieldId(e.target.value)}
              aria-label="Existing foreign-key field"
            >
              <option value="">— select a field —</option>
              {from.fields.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name} ({f.physicalType})
                </option>
              ))}
            </Select>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={confirm} disabled={!canConfirm}>
            Create relation
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
