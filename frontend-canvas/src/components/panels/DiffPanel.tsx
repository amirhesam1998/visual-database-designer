import { X } from "lucide-react";
import { asChangeColor, CHANGE_DOT } from "@/lib/diffStyle";
import { diffIsEmpty, useCanvasStore } from "@/store/canvasStore";

// A calm, readable view of the engine's operation list (spec §1/§4). The canvas computes nothing
// here — every line and colour comes straight from `/core/diff`. Moves never appear (the engine
// ignores `presentation`), so this only ever shows real schema changes.
export function DiffPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const diff = useCanvasStore((s) => s.diff);
  if (!open) return null;

  const empty = diffIsEmpty(diff);
  const stats = diff?.stats;

  return (
    <div className="absolute left-3 top-3 z-10 flex max-h-[calc(100%-1.5rem)] w-72 flex-col rounded-lg border border-border bg-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Changes</span>
          {stats && !empty && (
            <span className="text-2xs text-muted-foreground">
              +{stats.added} −{stats.removed} ~{stats.changed + stats.renamed}
            </span>
          )}
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground" aria-label="Close changes">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2" data-testid="diff-list">
        {empty ? (
          <p className="text-2xs text-muted-foreground">
            No changes vs the approved base. Moving tables isn’t a schema change.
          </p>
        ) : (
          <ul className="space-y-1">
            {diff!.colored.map((line, i) => (
              <li key={i} className="flex items-start gap-2 text-2xs">
                <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${CHANGE_DOT[asChangeColor(line.color)]}`} />
                <span className="font-mono leading-tight text-foreground">{line.text}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {diff && diff.notes.length > 0 && (
        <div className="border-t border-border px-3 py-2 text-2xs text-muted-foreground">{diff.notes[0]}</div>
      )}
    </div>
  );
}
