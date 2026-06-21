import { useReactFlow } from "reactflow";
import {
  AlertTriangle,
  CheckCircle2,
  Code2,
  DatabaseZap,
  GitCompare,
  ListChecks,
  Maximize2,
  Moon,
  Plus,
  Redo2,
  Search,
  Sparkles,
  Sun,
  Undo2,
  Upload,
  X,
  XCircle,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip } from "@/components/ui/tooltip";
import { diffIsEmpty, isDirty, severityCounts, useCanvasStore } from "@/store/canvasStore";

interface ToolbarProps {
  onApprove: () => void;
  onToggleDiff: () => void;
  onGenerate: () => void;
  onCode: () => void;
  onEnums: () => void;
  onImport: () => void;
  onDrift: () => void;
}

export function Toolbar({ onApprove, onToggleDiff, onGenerate, onCode, onEnums, onImport, onDrift }: ToolbarProps) {
  const { fitView, zoomIn, zoomOut, zoomTo } = useReactFlow();
  const search = useCanvasStore((s) => s.search);
  const setSearch = useCanvasStore((s) => s.setSearch);
  const theme = useCanvasStore((s) => s.theme);
  const toggleTheme = useCanvasStore((s) => s.toggleTheme);
  const model = useCanvasStore((s) => s.model);
  const editable = useCanvasStore((s) => s.editable);
  const addTable = useCanvasStore((s) => s.addTable);
  const undo = useCanvasStore((s) => s.undo);
  const redo = useCanvasStore((s) => s.redo);
  const canUndo = useCanvasStore((s) => s.past.length > 0);
  const canRedo = useCanvasStore((s) => s.future.length > 0);
  const validation = useCanvasStore((s) => s.validation);
  const status = useCanvasStore((s) => s.status);
  const diff = useCanvasStore((s) => s.diff);
  const approved = useCanvasStore((s) => s.approved);
  const dirty = useCanvasStore((s) => isDirty(s));

  const tableCount = model?.tables.length ?? 0;
  const relationCount = model?.relations.length ?? 0;
  const { errors, warnings } = severityCounts(validation);
  const changeCount = diff?.operations.length ?? 0;
  // Approve is enabled only when the engine has validated the changes with no errors (spec §2).
  const canApprove = !!validation?.valid && status === "ready" && !diffIsEmpty(diff);
  const approveHint = !validation?.valid
    ? "Fix validation errors before approving"
    : diffIsEmpty(diff)
      ? "No changes to approve"
      : "Review changes & approve";

  return (
    <header className="flex items-center gap-3 border-b border-border bg-card px-4 py-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">Database Canvas</span>
        <span className="rounded bg-muted px-1.5 py-0.5 text-2xs text-muted-foreground">
          {editable ? "editing" : "read-only"}
        </span>
        {dirty && (
          <Tooltip label="You have unsaved schema changes">
            <span className="flex items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-2xs font-medium text-amber-600 dark:text-amber-400">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> unsaved
            </span>
          </Tooltip>
        )}
        {approved && !dirty && (
          <Tooltip label={`Approved as ${approved.schemaVersion}`}>
            <span className="flex items-center gap-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-2xs font-medium text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-3 w-3" /> {approved.schemaVersion}
            </span>
          </Tooltip>
        )}
      </div>

      {model && (
        <span className="hidden text-2xs text-muted-foreground sm:inline">
          {tableCount} table{tableCount === 1 ? "" : "s"} · {relationCount} relation
          {relationCount === 1 ? "" : "s"}
        </span>
      )}

      {validation && (errors > 0 || warnings > 0) && (
        <div className="flex items-center gap-1.5 text-2xs">
          {errors > 0 && (
            <span className="flex items-center gap-1 text-destructive" aria-label={`${errors} errors`}>
              <XCircle className="h-3.5 w-3.5" /> {errors}
            </span>
          )}
          {warnings > 0 && (
            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400" aria-label={`${warnings} warnings`}>
              <AlertTriangle className="h-3.5 w-3.5" /> {warnings}
            </span>
          )}
        </div>
      )}

      <div className="relative ml-auto w-56">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search tables & fields…"
          className="pl-8 pr-8"
          aria-label="Search tables and fields"
        />
        {search && (
          <button
            onClick={() => setSearch("")}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            aria-label="Clear search"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {editable && (
        <>
          <Tooltip label="Generate a schema from a description">
            <Button variant="outline" size="sm" onClick={onGenerate} aria-label="Generate from description">
              <Sparkles className="h-4 w-4" /> Generate
            </Button>
          </Tooltip>
          <Tooltip label="Import or connect an existing database">
            <Button variant="outline" size="sm" onClick={onImport} aria-label="Import or connect a database">
              <Upload className="h-4 w-4" /> Import
            </Button>
          </Tooltip>
          <Tooltip label="Add table">
            <Button variant="outline" size="sm" onClick={() => void addTable("new_table")} aria-label="Add table">
              <Plus className="h-4 w-4" /> Table
            </Button>
          </Tooltip>
          <Tooltip label="Manage reusable enums">
            <Button variant="outline" size="icon" onClick={onEnums} aria-label="Manage enums">
              <ListChecks className="h-4 w-4" />
            </Button>
          </Tooltip>
          <div className="flex items-center">
            <Tooltip label="Undo">
              <Button variant="outline" size="icon" onClick={() => void undo()} disabled={!canUndo} aria-label="Undo">
                <Undo2 className="h-4 w-4" />
              </Button>
            </Tooltip>
            <Tooltip label="Redo">
              <Button variant="outline" size="icon" onClick={() => void redo()} disabled={!canRedo} aria-label="Redo">
                <Redo2 className="h-4 w-4" />
              </Button>
            </Tooltip>
          </div>
        </>
      )}

      <div className="flex items-center">
        <Tooltip label="Zoom out">
          <Button variant="outline" size="icon" onClick={() => zoomOut({ duration: 200 })} aria-label="Zoom out">
            <ZoomOut className="h-4 w-4" />
          </Button>
        </Tooltip>
        <Tooltip label="Reset zoom to 100%">
          <Button variant="outline" size="icon" onClick={() => zoomTo(1, { duration: 200 })} aria-label="Reset zoom to 100%">
            <span className="text-2xs font-semibold">1:1</span>
          </Button>
        </Tooltip>
        <Tooltip label="Zoom in">
          <Button variant="outline" size="icon" onClick={() => zoomIn({ duration: 200 })} aria-label="Zoom in">
            <ZoomIn className="h-4 w-4" />
          </Button>
        </Tooltip>
      </div>

      <Tooltip label="Fit to screen">
        <Button variant="outline" size="icon" onClick={() => fitView({ padding: 0.2, duration: 400 })} aria-label="Fit to screen">
          <Maximize2 className="h-4 w-4" />
        </Button>
      </Tooltip>

      <Tooltip label={theme === "dark" ? "Light theme" : "Dark theme"}>
        <Button variant="outline" size="icon" onClick={toggleTheme} aria-label="Toggle theme">
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </Tooltip>

      <Tooltip label="Compare your design with a live database">
        <Button variant="outline" size="sm" onClick={onDrift} aria-label="Compare with database" className="gap-1.5">
          <DatabaseZap className="h-4 w-4" /> Compare DB
        </Button>
      </Tooltip>

      <Tooltip label="Generate code (SQL, OpenAPI, ORM, CRUD)">
        <Button variant="outline" size="sm" onClick={onCode} aria-label="Open code panel" className="gap-1.5">
          <Code2 className="h-4 w-4" /> Code
        </Button>
      </Tooltip>

      <Tooltip label="Show changes vs the approved base">
        <Button variant="outline" size="sm" onClick={onToggleDiff} aria-label="Show changes" className="gap-1.5">
          <GitCompare className="h-4 w-4" />
          Changes
          {changeCount > 0 && (
            <span className="rounded-full bg-primary px-1.5 text-2xs text-primary-foreground">{changeCount}</span>
          )}
        </Button>
      </Tooltip>

      {/* Approve calls the engine gate (spec §2); enabled only when validated with no errors. */}
      <Tooltip label={approveHint}>
        {/* span wrapper so the tooltip still fires on a disabled button */}
        <span>
          <Button size="sm" onClick={onApprove} disabled={!canApprove} className="gap-1.5" aria-label="Approve">
            <CheckCircle2 className="h-4 w-4" />
            Approve
          </Button>
        </span>
      </Tooltip>
    </header>
  );
}
