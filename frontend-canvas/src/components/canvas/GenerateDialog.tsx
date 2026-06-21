import { useState } from "react";
import { Sparkles } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { generateSchemaFromPrd } from "@/lib/api";
import { useCanvasStore } from "@/store/canvasStore";

/** Generate a schema from a product description (unify spec phase 2 §1). The engine owns generation
 *  (deterministic core + optional LLM); the canvas only sends the PRD and loads what comes back. */
export function GenerateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const replaceDoc = useCanvasStore((s) => s.replaceDoc);
  const [prd, setPrd] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!prd.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const doc = await generateSchemaFromPrd(prd.trim());
      await replaceDoc(doc);
      setPrd("");
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title="Generate schema from description">
      <div className="space-y-3">
        <p className="text-2xs text-muted-foreground">
          Describe the product. The engine suggests a schema (Stable IDs + resolved types); it replaces
          the current canvas and you can keep editing. Nothing is decided in the browser.
        </p>
        <textarea
          value={prd}
          onChange={(e) => setPrd(e.target.value)}
          placeholder="e.g. A multi-tenant ticketing app with users, organizations, tickets, comments and labels…"
          className="h-32 w-full resize-y rounded-md border border-input bg-card px-2 py-1.5 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label="Product description"
        />
        {error && <p className="text-2xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={run} disabled={!prd.trim() || busy} className="gap-1.5">
            <Sparkles className="h-4 w-4" />
            {busy ? "Generating…" : "Generate"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
