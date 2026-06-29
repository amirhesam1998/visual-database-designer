import { useState } from "react";
import { useReactFlow } from "reactflow";
import { FileImage, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";
import { exportErd, type ErdFormat } from "@/lib/erdExport";
import { useCanvasStore } from "@/store/canvasStore";

// "Export ERD" — a separate toolbar control (the image comes from the canvas, not the engine Code
// panel). Download-only: SVG (vector), PNG (image), PDF (document). Display-only, never a schema change.
const FORMATS: { id: ErdFormat; label: string }[] = [
  { id: "svg", label: "SVG · vector" },
  { id: "png", label: "PNG · image" },
  { id: "pdf", label: "PDF · document" },
];

export function ErdExportMenu() {
  const { getNodes, fitView } = useReactFlow();
  const name = useCanvasStore((s) => (s.model?.meta?.name as string | undefined) ?? "erd");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<ErdFormat | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (format: ErdFormat) => {
    setBusy(format);
    setError(null);
    try {
      // Large maps render only on-screen tables (virtualisation); frame the whole diagram first so
      // every table is in the DOM and the capture isn't cropped (spec §1.2 — no cut-off ERD export).
      fitView({ padding: 0.1, duration: 0 });
      await new Promise((resolve) => setTimeout(resolve, 80));
      await exportErd(format, getNodes(), { name });
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
          <div className="absolute right-0 z-40 mt-1 w-44 rounded-md border border-border bg-card p-1 shadow-xl">
            {FORMATS.map((f) => (
              <button
                key={f.id}
                onClick={() => void run(f.id)}
                disabled={!!busy}
                className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-muted disabled:opacity-50"
              >
                {f.label}
                {busy === f.id && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              </button>
            ))}
            {error && <p className="px-2 py-1 text-2xs text-destructive">{error}</p>}
          </div>
        </>
      )}
    </div>
  );
}
