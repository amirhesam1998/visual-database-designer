import { useCallback, useEffect, useState } from "react";
import { ReactFlowProvider } from "reactflow";
import { Canvas } from "@/components/canvas/Canvas";
import { ApproveDialog } from "@/components/canvas/ApproveDialog";
import { GenerateDialog } from "@/components/canvas/GenerateDialog";
import { EnumDialog } from "@/components/canvas/EnumDialog";
import { ImportDialog } from "@/components/canvas/ImportDialog";
import { Toolbar } from "@/components/panels/Toolbar";
import { CodePanel } from "@/components/panels/CodePanel";
import { DriftPanel } from "@/components/panels/DriftPanel";
import { InsightsPanel } from "@/components/panels/InsightsPanel";
import { DetailsPanel } from "@/components/panels/DetailsPanel";
import { DiffPanel } from "@/components/panels/DiffPanel";
import { EmptyState, ErrorState, LoadingState } from "@/components/panels/States";
import { useCanvasStore } from "@/store/canvasStore";
import { SAMPLE_SCHEMA_JSON } from "@/lib/sample";
import type { RenderRequest } from "@/lib/api";

/** Where does the schema come from? `?sessionId=…` renders a live design session; otherwise we show
 *  a bundled sample so the page is never blank — both go through the same `/design/render` endpoint. */
function sourceFromUrl(): RenderRequest {
  const params = new URLSearchParams(window.location.search);
  const sessionId = params.get("sessionId");
  if (sessionId) return { sessionId };
  return { schemaJson: SAMPLE_SCHEMA_JSON };
}

export default function App() {
  const status = useCanvasStore((s) => s.status);
  const error = useCanvasStore((s) => s.error);
  const model = useCanvasStore((s) => s.model);
  const load = useCanvasStore((s) => s.load);

  const reload = useCallback(() => void load(sourceFromUrl()), [load]);
  useEffect(() => {
    reload();
  }, [reload]);

  const [showDiff, setShowDiff] = useState(false);
  const [showApprove, setShowApprove] = useState(false);
  const [showGenerate, setShowGenerate] = useState(false);
  const [showCode, setShowCode] = useState(false);
  const [showEnums, setShowEnums] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showDrift, setShowDrift] = useState(false);
  const [showInsights, setShowInsights] = useState(false);

  // Code, Drift and Insights share the right-hand dock, so opening one closes the others.
  const closeRightDock = () => {
    setShowCode(false);
    setShowDrift(false);
    setShowInsights(false);
  };

  return (
    <ReactFlowProvider>
      <div className="flex h-full flex-col">
        <Toolbar
          onApprove={() => setShowApprove(true)}
          onToggleDiff={() => setShowDiff((v) => !v)}
          onGenerate={() => setShowGenerate(true)}
          onCode={() => { const next = !showCode; closeRightDock(); setShowCode(next); }}
          onEnums={() => setShowEnums(true)}
          onImport={() => setShowImport(true)}
          onDrift={() => { const next = !showDrift; closeRightDock(); setShowDrift(next); }}
          onInsights={() => { const next = !showInsights; closeRightDock(); setShowInsights(next); }}
        />
        <div className="flex min-h-0 flex-1">
          <main className="relative min-w-0 flex-1">
            {status === "loading" && <LoadingState />}
            {status === "error" && <ErrorState message={error ?? "Unknown error"} onRetry={reload} />}
            {status === "ready" && model && model.tables.length === 0 && <EmptyState />}
            {status === "ready" && model && model.tables.length > 0 && <Canvas model={model} />}
            <DiffPanel open={showDiff} onClose={() => setShowDiff(false)} />
            <CodePanel open={showCode} onClose={() => setShowCode(false)} />
            <DriftPanel open={showDrift} onClose={() => setShowDrift(false)} />
            <InsightsPanel open={showInsights} onClose={() => setShowInsights(false)} />
          </main>
          <DetailsPanel />
        </div>
      </div>
      <ApproveDialog open={showApprove} onClose={() => setShowApprove(false)} />
      <GenerateDialog open={showGenerate} onClose={() => setShowGenerate(false)} />
      <EnumDialog open={showEnums} onClose={() => setShowEnums(false)} />
      <ImportDialog open={showImport} onClose={() => setShowImport(false)} />
    </ReactFlowProvider>
  );
}
