import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, ShieldAlert } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { riskSchema } from "@/lib/api";
import { diffIsEmpty, useCanvasStore } from "@/store/canvasStore";
import type { ApproveResult, RiskReport } from "@/lib/types";

// Approve = call the engine gate (spec §2). This dialog only *shows* the diff + the engine's risk
// (spec §3) and reflects the gate's guards — it decides nothing. A `critical` migration (e.g. drop
// table) requires an explicit acknowledgement before the gate will accept it (the engine's
// `acknowledgeCritical`), surfaced here as a checkbox — never bypassed.

const LEVEL_TEXT: Record<string, string> = {
  critical: "text-rose-600 dark:text-rose-400",
  high: "text-rose-600 dark:text-rose-400",
  medium: "text-amber-600 dark:text-amber-400",
  low: "text-muted-foreground",
  safe: "text-emerald-600 dark:text-emerald-400",
};

export function ApproveDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const baseline = useCanvasStore((s) => s.baseline);
  const doc = useCanvasStore((s) => s.doc);
  const diff = useCanvasStore((s) => s.diff);
  const runApprove = useCanvasStore((s) => s.runApprove);

  const [risk, setRisk] = useState<RiskReport | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ApproveResult | null>(null);

  // Pull the engine's risk report for display whenever the dialog opens (display, not decision).
  useEffect(() => {
    if (!open || !baseline || !doc) return;
    setRisk(null);
    setAcknowledged(false);
    setResult(null);
    let active = true;
    riskSchema(baseline, doc)
      .then((r) => active && setRisk(r))
      .catch(() => active && setRisk(null));
    return () => {
      active = false;
    };
  }, [open, baseline, doc]);

  if (!open) return null;

  const isCritical = risk?.max_level === "critical";
  const criticalOps = (risk?.operations ?? []).filter((o) => o.level === "critical" || o.level === "high");
  const needsAck = isCritical && !acknowledged;
  const stats = diff?.stats;

  const approve = async () => {
    setBusy(true);
    const r = await runApprove(acknowledged);
    setResult(r);
    setBusy(false);
    if (r.status === "approved") setTimeout(onClose, 1400);
  };

  // ---- success ---------------------------------------------------------------------------------
  if (result?.status === "approved") {
    return (
      <Dialog open onClose={onClose} title="Approved">
        <div className="flex flex-col items-center gap-2 py-4 text-center">
          <CheckCircle2 className="h-10 w-10 text-emerald-500" />
          <p className="text-sm font-medium">Schema {result.schemaVersion} approved and locked.</p>
          <p className="break-all font-mono text-2xs text-muted-foreground">{result.checksum}</p>
          <p className="text-2xs text-muted-foreground">This version is now the base for the next changes.</p>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog open onClose={onClose} title="Review & approve">
      <div className="space-y-3 text-xs">
        {diffIsEmpty(diff) ? (
          <p className="text-muted-foreground">No schema changes to approve.</p>
        ) : (
          <div>
            <div className="mb-1 font-medium">Changes</div>
            {stats && (
              <p className="text-2xs text-muted-foreground">
                {stats.added} added · {stats.removed} removed · {stats.changed + stats.renamed} changed
              </p>
            )}
          </div>
        )}

        <div>
          <div className="mb-1 font-medium">Migration risk</div>
          {risk === null ? (
            <p className="flex items-center gap-1 text-2xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" /> analysing…
            </p>
          ) : (
            <>
              <p className={`text-2xs font-semibold uppercase ${LEVEL_TEXT[risk.max_level] ?? ""}`}>
                {risk.max_level} risk
              </p>
              {criticalOps.length > 0 && (
                <ul className="mt-1 space-y-1">
                  {criticalOps.map((o, i) => (
                    <li key={i} className="flex items-start gap-1 text-2xs text-muted-foreground">
                      <AlertTriangle className="mt-px h-3 w-3 shrink-0 text-rose-500" />
                      <span>
                        <span className="font-mono">{o.op}</span>
                        {o.target ? ` ${o.target}` : ""} — {o.explanation.en}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>

        {isCritical && (
          <label className="flex items-start gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-2">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              className="mt-0.5"
              aria-label="Acknowledge critical migration risk"
            />
            <span className="flex items-center gap-1 text-2xs text-rose-700 dark:text-rose-300">
              <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
              I understand this includes a <b>critical</b>, irreversible operation and want to proceed.
            </span>
          </label>
        )}

        {result && (
          <p className="rounded-md bg-destructive/10 px-2 py-1.5 text-2xs text-destructive">
            {result.status === "validation_error"
              ? "Blocked: the schema has validation errors — fix them before approving."
              : result.status === "critical_risk"
                ? "Blocked: a critical operation needs explicit acknowledgement."
                : `Approve failed: ${result.message}`}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={approve} disabled={busy || needsAck} className="gap-1.5">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            Approve
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
