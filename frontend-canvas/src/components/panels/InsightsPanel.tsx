import { useMemo } from "react";
import { AlertTriangle, Database, Info, Lightbulb, Lock, ShieldCheck, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { insightCounts, useCanvasStore } from "@/store/canvasStore";
import type { Insight, InsightCategory, InsightSeverity } from "@/lib/types";

// The Insights panel surfaces the engine's design-assistant analysis (intelligence milestone). It
// computes nothing — every finding, its "why" and its severity come straight from `/design/insights`.
// The spec's hard rule is the fact/suggestion split (§0): certain issues are shown separately from
// heuristic guesses, so the user always knows what's a fact and what's a suggestion to confirm.

const SEVERITY_ORDER: Record<InsightSeverity, number> = { error: 0, warning: 1, info: 2 };

const CATEGORY_META: Record<InsightCategory, { label: string; icon: typeof Database }> = {
  index: { label: "index", icon: Database },
  design: { label: "design", icon: AlertTriangle },
  privacy: { label: "privacy", icon: Lock },
};

function SeverityIcon({ severity }: { severity: InsightSeverity }) {
  if (severity === "error") return <XCircle className="h-3.5 w-3.5 shrink-0 text-rose-500" />;
  if (severity === "warning") return <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-500" />;
  return <Info className="h-3.5 w-3.5 shrink-0 text-sky-500" />;
}

function InsightRow({ insight }: { insight: Insight }) {
  const select = useCanvasStore((s) => s.select);
  const applyInsight = useCanvasStore((s) => s.applyInsight);
  const cat = CATEGORY_META[insight.category];
  const CatIcon = cat?.icon ?? Info;

  return (
    <li
      className="rounded-md border border-border/70 px-2.5 py-2"
      data-testid={`insight-${insight.rule_id}`}
      data-rule={insight.rule_id}
    >
      <div className="flex items-start gap-2">
        <SeverityIcon severity={insight.severity} />
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={() => insight.table_id && select(insight.table_id)}
            className={cn(
              "block text-left text-xs font-medium leading-snug text-foreground",
              insight.table_id && "hover:underline",
            )}
            title={insight.table_id ? "Show on canvas" : undefined}
          >
            {insight.title}
          </button>
          <p className="mt-0.5 text-2xs leading-snug text-muted-foreground">{insight.why}</p>
          {insight.fix && (
            <p className="mt-0.5 text-2xs leading-snug text-muted-foreground/80">
              <span className="font-medium">Fix:</span> {insight.fix}
            </p>
          )}
        </div>
        {cat && (
          <span className="flex shrink-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            <CatIcon className="h-3 w-3" /> {cat.label}
          </span>
        )}
      </div>
      {insight.action && (
        <div className="mt-1.5 flex justify-end">
          <Button
            variant="outline"
            size="sm"
            className="h-6"
            onClick={() => void applyInsight(insight)}
            aria-label={`${insight.action.label} for ${insight.title}`}
          >
            {insight.action.label}
          </Button>
        </div>
      )}
    </li>
  );
}

function Group({ title, hint, items }: { title: string; hint: string; items: Insight[] }) {
  if (items.length === 0) return null;
  const sorted = [...items].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  return (
    <section className="px-3 py-2">
      <div className="mb-1.5">
        <h3 className="text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
          {title} ({items.length})
        </h3>
        <p className="text-[10px] leading-tight text-muted-foreground/70">{hint}</p>
      </div>
      <ul className="space-y-1.5">
        {sorted.map((i, idx) => (
          <InsightRow key={`${i.rule_id}-${i.entity_id ?? idx}`} insight={i} />
        ))}
      </ul>
    </section>
  );
}

export function InsightsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const insights = useCanvasStore((s) => s.insights);
  const validating = useCanvasStore((s) => s.validating);
  const { facts, suggestions } = useMemo(() => insightCounts(insights), [insights]);

  if (!open) return null;

  const factItems = insights.filter((i) => i.kind === "fact");
  const suggestionItems = insights.filter((i) => i.kind === "suggestion");

  return (
    <aside className="absolute inset-y-0 right-0 z-20 flex w-[28rem] max-w-full flex-col border-l border-border bg-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Lightbulb className="h-4 w-4 text-amber-500" />
          <span className="text-sm font-semibold">Insights</span>
          <span className="text-2xs text-muted-foreground">
            {facts} issue{facts === 1 ? "" : "s"} · {suggestions} suggestion{suggestions === 1 ? "" : "s"}
          </span>
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground" aria-label="Close insights">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="insights-list">
        {insights.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <ShieldCheck className="h-8 w-8 text-emerald-500/70" />
            <p className="text-sm font-medium">Nothing to flag</p>
            <p className="text-2xs text-muted-foreground">
              {validating ? "Analysing the schema…" : "No index, design or privacy insights for this schema."}
            </p>
          </div>
        ) : (
          <>
            <Group
              title="Issues"
              hint="Certain, derived from the structure. Worth fixing."
              items={factItems}
            />
            <div className="border-t border-border" />
            <Group
              title="Suggestions"
              hint="Heuristic guesses to confirm or dismiss — nothing is applied automatically."
              items={suggestionItems}
            />
          </>
        )}
      </div>

      <div className="border-t border-border px-4 py-2 text-2xs text-muted-foreground">
        Analysis is deterministic and engine-side. Applying a suggestion is a normal, validated edit.
      </div>
    </aside>
  );
}
