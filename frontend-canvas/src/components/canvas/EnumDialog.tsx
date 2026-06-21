import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCanvasStore } from "@/store/canvasStore";
import type { SchemaEnum } from "@/lib/schema";

// Manage reusable named enums on the engine's `logical.enums` (unify spec phase 2 §B). Structural:
// every change goes through the same mutate → render → validate round-trip. Fields attach to an enum
// from the details panel; this dialog owns the enum definitions themselves.
function EnumRow({ def }: { def: SchemaEnum }) {
  const updateEnum = useCanvasStore((s) => s.updateEnum);
  const removeEnum = useCanvasStore((s) => s.removeEnum);
  const [name, setName] = useState(def.name);
  const [values, setValues] = useState(def.values.map((v) => v.value).join(", "));

  const commitName = () => {
    const trimmed = name.trim();
    if (trimmed && trimmed !== def.name) updateEnum(def.id, { name: trimmed });
    else setName(def.name);
  };
  const commitValues = () => {
    const parsed = values.split(",").map((v) => v.trim()).filter(Boolean);
    updateEnum(def.id, { values: parsed });
  };

  return (
    <li className="rounded-md border border-border/70 px-2 py-2">
      <div className="flex items-center gap-1.5">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          aria-label={`Enum name for ${def.name}`}
          className="h-7 flex-1 text-xs font-medium"
        />
        <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => removeEnum(def.id)} aria-label={`Delete enum ${def.name}`}>
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
      <Input
        value={values}
        onChange={(e) => setValues(e.target.value)}
        onBlur={commitValues}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        placeholder="comma,separated,values"
        aria-label={`Values for ${def.name}`}
        className="mt-1.5 h-7 text-xs"
      />
    </li>
  );
}

export function EnumDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const doc = useCanvasStore((s) => s.doc);
  const addEnum = useCanvasStore((s) => s.addEnum);
  const defs = doc?.logical.enums ?? [];

  return (
    <Dialog open={open} onClose={onClose} title="Reusable enums">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-2xs text-muted-foreground">
            Named value sets stored on the schema. Attach one to a field from its type dropdown.
          </p>
          <Button variant="outline" size="sm" onClick={() => addEnum("new_enum", [])} className="gap-1" aria-label="Add enum">
            <Plus className="h-3.5 w-3.5" /> Enum
          </Button>
        </div>
        {defs.length === 0 ? (
          <p className="py-4 text-center text-2xs text-muted-foreground">No enums yet.</p>
        ) : (
          <ul className="max-h-80 space-y-2 overflow-y-auto">
            {defs.map((d) => (
              <EnumRow key={d.id} def={d} />
            ))}
          </ul>
        )}
      </div>
    </Dialog>
  );
}
