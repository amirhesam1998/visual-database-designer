import { useState } from "react";
import { useReactFlow } from "reactflow";
import { FileImage, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";
import { exportErd, type ErdFormat } from "@/lib/erdExport";
import { resolvePositions, type Positions } from "@/lib/layout";
import { useCanvasStore } from "@/store/canvasStore";

// "Export ERD" — a separate toolbar control (the image is rendered from the schema data + canvas
// layout, not the engine Code panel). Download-only: SVG (vector, any scale), PNG (image), and PDF
// either on one scaled page or tiled across multiple real-size pages for big maps. Display-only.
interface ExportItem {
  id: string;
  label: string;
  format: ErdFormat;
  paged?: boolean;
}

const ITEMS: ExportItem[] = [
  { id: "svg", label: "SVG · vector", format: "svg" },
  { id: "png", label: "PNG · image", format: "png" },
  { id: "pdf", label: "PDF · one page", format: "pdf" },
  { id: "pdf-multi", label: "PDF · multi-page", format: "pdf", paged: true },
];

export function ErdExportMenu() {
  const { getNodes } = useReactFlow();
  const model = useCanvasStore((s) => s.model);
  const name = (model?.meta?.name as string | undefined) ?? "erd";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (item: ExportItem) => {
    setBusy(item.id);
    setError(null);
    try {
      if (!model || model.tables.length === 0) {
        throw new Error("Nothing to export — add some tables to the canvas first.");
      }
      // Start from the deterministic layout (so every table has a position even if off-screen), then
      // override with the live canvas positions — the export matches exactly what the user sees, and a
      // huge map is never cropped because we read positions from data, not the rendered DOM (spec §1).
      const positions: Positions = { ...resolvePositions(model) };
      for (const n of getNodes()) positions[n.id] = { x: n.position.x, y: n.position.y };
      await exportErd(item.format, model, positions, { name, paged: item.paged });
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="relative">
      <Tooltip label="Export the diagram as an image (SVG / PNG / PDF)">
        <Button variant="outline" size="sm" onClick={() => setOpen((v) => !v)} aria-label="Export ERD image" className="gap-1.5">
          <FileImage className="h-4 w-4" /> ERD
        </Button>
      </Tooltip>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} role="presentation" />
          <div className="absolute right-0 z-40 mt-1 w-48 rounded-md border border-border bg-card p-1 shadow-xl">
            {ITEMS.map((item) => (
              <button
                key={item.id}
                onClick={() => void run(item)}
                disabled={!!busy}
                className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-muted disabled:opacity-50"
              >
                {item.label}
                {busy === item.id && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              </button>
            ))}
            {error && <p className="px-2 py-1 text-2xs text-destructive">{error}</p>}
          </div>
        </>
      )}
    </div>
  );
}
