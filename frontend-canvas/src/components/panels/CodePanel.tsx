import { useEffect, useMemo, useState } from "react";
import { Check, Copy, Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import {
  fetchCodeFrameworks,
  generateCode,
  generateOpenApi,
  type CodeFrameworks,
  type CodeKind,
} from "@/lib/api";
import { downloadText } from "@/lib/download";
import { useCanvasStore } from "@/store/canvasStore";

// The Code panel surfaces the engine's generation (unify spec phase 2 + export-formats milestone). It
// NEVER generates anything itself: `sql`/`openapi` and the text exports (`yaml`/`dbml`/`jsonschema`/
// `datadict`) are engine-native; `model`/`crud`/`schema` go through the server-side bridge that reuses
// the proven generators. The panel only picks options, shows the returned text, and copies/downloads it.
type Kind = CodeKind | "openapi";

const KIND_LABEL: Record<Kind, string> = {
  sql: "SQL (DDL)",
  openapi: "OpenAPI 3.1",
  model: "ORM model",
  crud: "CRUD controller",
  schema: "Schema export",
  yaml: "YAML",
  dbml: "DBML (dbdiagram)",
  jsonschema: "JSON Schema",
  datadict: "Data dictionary",
};

// File extension + MIME for the Download button (the artifact is plain text either way).
const DOWNLOAD_AS: Record<Kind, { ext: string; mime: string }> = {
  sql: { ext: "sql", mime: "text/plain" },
  openapi: { ext: "json", mime: "application/json" },
  model: { ext: "txt", mime: "text/plain" },
  crud: { ext: "txt", mime: "text/plain" },
  schema: { ext: "txt", mime: "text/plain" },
  yaml: { ext: "yaml", mime: "text/yaml" },
  dbml: { ext: "dbml", mime: "text/plain" },
  jsonschema: { ext: "schema.json", mime: "application/json" },
  datadict: { ext: "md", mime: "text/markdown" },
};

export function CodePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const doc = useCanvasStore((s) => s.doc);
  const model = useCanvasStore((s) => s.model);

  const [frameworks, setFrameworks] = useState<CodeFrameworks | null>(null);
  const [kind, setKind] = useState<Kind>("sql");
  const [framework, setFramework] = useState("");
  const [driver, setDriver] = useState("postgres");
  const [table, setTable] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!open || frameworks) return;
    fetchCodeFrameworks().then(setFrameworks).catch(() => undefined);
  }, [open, frameworks]);

  const frameworkOptions = useMemo(() => {
    if (!frameworks) return [];
    if (kind === "model") return frameworks.model;
    if (kind === "crud") return frameworks.crud;
    if (kind === "schema") return frameworks.schema;
    return [];
  }, [frameworks, kind]);

  // Keep the framework selection valid whenever the kind changes.
  useEffect(() => {
    if (frameworkOptions.length && !frameworkOptions.includes(framework)) setFramework(frameworkOptions[0]);
  }, [frameworkOptions, framework]);

  const needsTable = kind === "model" || kind === "crud";
  const tables = model?.tables ?? [];

  const run = async () => {
    if (!doc || busy) return;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const tableName = needsTable ? table || tables[0]?.name : undefined;
      const out =
        kind === "openapi"
          ? await generateOpenApi(doc)
          : await generateCode({
              schemaJson: doc, kind, framework: framework || undefined, table: tableName,
              driver: kind === "sql" ? driver : undefined,
            });
      setContent(out);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setContent("");
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!content) return;
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  const download = () => {
    if (!content) return;
    const { ext, mime } = DOWNLOAD_AS[kind];
    const base = (kind === "model" || kind === "crud" ? table || tables[0]?.name : kind) || "export";
    downloadText(`${base}.${ext}`, content, mime);
  };

  if (!open) return null;

  return (
    <aside className="absolute inset-y-0 right-0 z-20 flex w-[28rem] max-w-full flex-col border-l border-border bg-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Code</h2>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close code panel">
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-2 border-b border-border px-4 py-3">
        <label className="flex flex-col gap-1 text-2xs text-muted-foreground">
          Artifact
          <Select value={kind} onChange={(e) => setKind(e.target.value as Kind)} aria-label="Code artifact" className="w-36">
            {(Object.keys(KIND_LABEL) as Kind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </Select>
        </label>
        {kind === "sql" && (
          <label className="flex flex-col gap-1 text-2xs text-muted-foreground">
            Database
            <Select value={driver} onChange={(e) => setDriver(e.target.value)} aria-label="Target database" className="w-32">
              {(frameworks?.sql ?? ["postgres", "mysql"]).map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </Select>
          </label>
        )}
        {frameworkOptions.length > 0 && (
          <label className="flex flex-col gap-1 text-2xs text-muted-foreground">
            Framework
            <Select value={framework} onChange={(e) => setFramework(e.target.value)} aria-label="Framework" className="w-32">
              {frameworkOptions.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </Select>
          </label>
        )}
        {needsTable && (
          <label className="flex flex-col gap-1 text-2xs text-muted-foreground">
            Table
            <Select value={table} onChange={(e) => setTable(e.target.value)} aria-label="Table" className="w-32">
              {tables.map((t) => (
                <option key={t.id} value={t.name}>
                  {t.name}
                </option>
              ))}
            </Select>
          </label>
        )}
        <Button size="sm" onClick={run} disabled={!doc || busy} aria-label="Generate code">
          {busy ? "Generating…" : "Generate"}
        </Button>
      </div>

      <div className="relative min-h-0 flex-1 overflow-auto">
        {error && <p className="p-4 text-2xs text-destructive">{error}</p>}
        {!error && content && (
          <>
            <div className="absolute right-3 top-3 flex gap-1.5">
              <Button variant="outline" size="sm" onClick={download} aria-label="Download" className="gap-1.5">
                <Download className="h-3.5 w-3.5" />
                Download
              </Button>
              <Button variant="outline" size="sm" onClick={copy} aria-label="Copy code" className="gap-1.5">
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <pre className="overflow-x-auto p-4 pt-12 font-mono text-2xs leading-relaxed text-foreground">{content}</pre>
          </>
        )}
        {!error && !content && !busy && (
          <p className="p-4 text-2xs text-muted-foreground">
            Pick an artifact and press Generate. Everything is produced by the engine — SQL and OpenAPI
            natively, ORM/CRUD/exports through the server-side bridge.
          </p>
        )}
      </div>
    </aside>
  );
}
