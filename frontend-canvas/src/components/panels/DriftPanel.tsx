import { useState } from "react";
import { Database, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { driftIsEmpty, useCanvasStore } from "@/store/canvasStore";
import type { DriftEntry } from "@/lib/types";

// "Compare with database" (database-connection milestone §3). The engine does the three-way drift
// (designed ↔ migrations ↔ live) and categorises every difference; this panel only sends the working
// design + a live DSN and renders the report. It also reflects onto the canvas (see driftColors). It
// never writes to the database — import + compare only (AD-5).

// Human-readable label + dot colour per drift category (matches the canvas tint language).
const CATEGORY: Record<string, { label: string; dot: string }> = {
  design_ahead_of_code: { label: "Only in your design", dot: "bg-emerald-500" },
  migration_not_applied: { label: "Designed, not yet in the database", dot: "bg-emerald-500" },
  migration_incomplete: { label: "Type differs", dot: "bg-amber-500" },
  manual_prod_change: { label: "Only in the database (manual change)", dot: "bg-rose-500" },
  code_ahead_of_design: { label: "In the database, not in your design", dot: "bg-rose-500" },
};

function categoryOf(c: string) {
  return CATEGORY[c] ?? { label: c, dot: "bg-muted-foreground" };
}

function DriftRow({ d }: { d: DriftEntry }) {
  const { dot } = categoryOf(d.category);
  return (
    <li className="flex items-start gap-2 text-2xs">
      <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${dot}`} />
      <span className="min-w-0">
        <span className="font-mono font-medium text-foreground">{d.entity}</span>
        {d.detail && <span className="block leading-tight text-muted-foreground">{d.detail}</span>}
      </span>
    </li>
  );
}

export function DriftPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const drift = useCanvasStore((s) => s.drift);
  const driftBusy = useCanvasStore((s) => s.driftBusy);
  const compareWithDatabase = useCanvasStore((s) => s.compareWithDatabase);
  const clearDrift = useCanvasStore((s) => s.clearDrift);
  const [dsn, setDsn] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const run = async () => {
    if (!dsn.trim() || driftBusy) return;
    setError(null);
    const res = await compareWithDatabase(dsn.trim());
    if (!res.ok) setError(res.error ?? "compare failed");
  };

  const close = () => {
    setDsn(""); // the connection string is sensitive — don't keep it after close (spec §1)
    setError(null);
    onClose();
  };

  const grouped = new Map<string, DriftEntry[]>();
  for (const d of drift?.drift ?? []) {
    const key = categoryOf(d.category).label;
    (grouped.get(key) ?? grouped.set(key, []).get(key)!).push(d);
  }

  return (
    <aside className="absolute inset-y-0 right-0 z-20 flex w-[28rem] max-w-full flex-col border-l border-border bg-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Compare with database</h2>
        <Button variant="ghost" size="icon" onClick={close} aria-label="Close drift panel">
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="space-y-2 border-b border-border px-4 py-3">
        <p className="text-2xs text-muted-foreground">
          Three-way drift (your design ↔ migrations ↔ live) — computed by the engine, read-only. Nothing
          is written to the database.
        </p>
        <div className="flex items-end gap-2">
          <label className="flex-1 text-2xs font-medium text-muted-foreground">
            Live database connection
            <Input
              value={dsn}
              onChange={(e) => setDsn(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void run()}
              placeholder="postgresql://user:pass@host:5432/dbname"
              aria-label="Live database connection string"
              className="mt-1 font-mono"
            />
          </label>
          <Button size="sm" onClick={run} disabled={!dsn.trim() || driftBusy} className="gap-1.5" aria-label="Compare">
            {driftBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            Compare
          </Button>
        </div>
        {error && <p className="text-2xs text-destructive">{error}</p>}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="drift-list">
        {!drift && !error && (
          <p className="text-2xs text-muted-foreground">
            Enter a database connection and press Compare to see what differs from your design.
          </p>
        )}
        {drift && driftIsEmpty(drift) && (
          <p className="text-2xs text-emerald-600 dark:text-emerald-400">
            In sync — your design matches the database. {drift.reconcile.matched} table
            {drift.reconcile.matched === 1 ? "" : "s"} matched.
          </p>
        )}
        {drift && !driftIsEmpty(drift) && (
          <div className="space-y-3">
            {[...grouped.entries()].map(([label, entries]) => (
              <section key={label}>
                <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {label} <span className="font-normal">({entries.length})</span>
                </h3>
                <ul className="space-y-1.5">
                  {entries.map((d, i) => <DriftRow key={`${d.entity}-${i}`} d={d} />)}
                </ul>
              </section>
            ))}
            {drift.reconcile.ambiguous.length > 0 && (
              <section>
                <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
                  Needs your confirmation ({drift.reconcile.ambiguous.length})
                </h3>
                <ul className="space-y-1.5">
                  {drift.reconcile.ambiguous.map((m, i) => (
                    <li key={i} className="text-2xs text-muted-foreground">
                      <span className="font-mono text-foreground">{m.entity}</span> ↔ {m.candidates.join(", ")} — {m.reason}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}
      </div>

      {drift && (
        <div className="flex items-center justify-between border-t border-border px-4 py-2 text-2xs text-muted-foreground">
          <span>{drift.drift.length} difference{drift.drift.length === 1 ? "" : "s"}</span>
          <button onClick={clearDrift} className="hover:text-foreground" aria-label="Clear drift">Clear</button>
        </div>
      )}
    </aside>
  );
}
