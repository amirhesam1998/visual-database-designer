import { useState } from "react";
import { BookmarkPlus, Database, FileUp, Loader2, Plug, X } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { importLiveDatabase, importSqlDump } from "@/lib/api";
import { useCanvasStore } from "@/store/canvasStore";
import { useSavedConnections, type SavedConnection } from "@/store/savedConnections";
import type { ImportResult, ImportSuggestion } from "@/lib/types";
import type { SchemaDoc } from "@/lib/schema";

// Open an existing database in the designer (database-connection milestone §1/§2). Two sources, both
// engine-backed: a live Postgres connection or a SQL/DDL file (applied to a shadow DB server-side).
// The browser parses no SQL and infers no types — it sends the request and loads what the engine
// returns (golden rule). Ambiguous reverse-inferences are surfaced for the human to confirm (AD-5),
// and the imported map becomes a brownfield baseline so edits diff/approve against the real database.

type Mode = "connect" | "file";

/** Build a connection DSN from the parts, or pass the raw connection string straight through. The
 *  scheme + default port follow the selected driver (Postgres 5432 / MySQL 3306). */
function assembleDsn(
  p: { dsn: string; host: string; port: string; database: string; user: string; password: string },
  driver: string,
): string {
  if (p.dsn.trim()) return p.dsn.trim();
  const auth = p.user ? `${encodeURIComponent(p.user)}${p.password ? `:${encodeURIComponent(p.password)}` : ""}@` : "";
  const host = p.host.trim() || "localhost";
  const isMysql = driver === "mysql";
  const port = p.port.trim() || (isMysql ? "3306" : "5432");
  const scheme = isMysql ? "mysql" : "postgresql";
  return `${scheme}://${auth}${host}:${port}/${p.database.trim() || (isMysql ? "mysql" : "postgres")}`;
}

export function ImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const replaceDoc = useCanvasStore((s) => s.replaceDoc);
  const savedConnections = useSavedConnections((s) => s.connections);
  const saveConnection = useSavedConnections((s) => s.save);
  const removeConnection = useSavedConnections((s) => s.remove);

  const [mode, setMode] = useState<Mode>("connect");
  const [driver, setDriver] = useState("postgres");
  const [dsn, setDsn] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [database, setDatabase] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [fileName, setFileName] = useState("");
  const [sql, setSql] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<ImportResult | null>(null); // imported, awaiting confirm

  const reset = () => {
    setError(null);
    setPending(null);
  };

  // Fill the form from a saved connection. The password is never stored, so it's left blank to
  // re-enter (spec §3). Clearing the DSN field makes the "parts" the active source.
  const loadConnection = (c: SavedConnection) => {
    setDsn("");
    setDriver(c.driver);
    setHost(c.host); setPort(c.port); setDatabase(c.database); setUser(c.user);
    setPassword("");
    reset();
  };

  // Save the current connection parts (never the password). Available once a host/database is entered.
  const onSaveConnection = () => {
    if (!host.trim() && !database.trim()) return;
    const label = `${user.trim() || "db"}@${host.trim() || "localhost"}/${database.trim()}`.replace(/\/$/, "");
    saveConnection({ label, driver, host: host.trim(), port: port.trim(), database: database.trim(), user: user.trim() });
  };

  const close = () => {
    // The connection string is sensitive — never keep it around once the dialog closes (spec §1).
    setDsn(""); setHost(""); setPort(""); setDatabase(""); setUser(""); setPassword("");
    setFileName(""); setSql(""); setDriver("postgres"); reset(); setBusy(false);
    onClose();
  };

  const runImport = async () => {
    if (busy) return;
    reset();
    setBusy(true);
    try {
      const result =
        mode === "connect"
          ? await importLiveDatabase(assembleDsn({ dsn, host, port, database, user, password }, driver), undefined, driver)
          : await importSqlDump(sql, undefined, driver);
      const suggestions = result.inference?.suggestions ?? [];
      if (suggestions.length > 0) {
        setPending(result); // pause on ambiguous types for the human to confirm (AD-5)
      } else {
        await loadResult(result);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const loadResult = async (result: ImportResult) => {
    await replaceDoc(result.schema_json as unknown as SchemaDoc);
    close();
  };

  const onPickFile = async (file: File | undefined) => {
    if (!file) return;
    setFileName(file.name);
    setSql(await file.text());
    reset();
  };

  const canImport = mode === "connect"
    ? !!(dsn.trim() || host.trim() || database.trim())
    : !!sql.trim();

  return (
    <Dialog open={open} onClose={close} title="Import / connect a database" className="max-w-lg">
      <div className="space-y-3">
        <div className="flex gap-1 rounded-md bg-muted p-1 text-xs">
          <button
            className={`flex flex-1 items-center justify-center gap-1.5 rounded px-2 py-1.5 ${mode === "connect" ? "bg-card font-medium shadow-sm" : "text-muted-foreground"}`}
            onClick={() => { setMode("connect"); reset(); }}
          >
            <Plug className="h-3.5 w-3.5" /> Live connection
          </button>
          <button
            className={`flex flex-1 items-center justify-center gap-1.5 rounded px-2 py-1.5 ${mode === "file" ? "bg-card font-medium shadow-sm" : "text-muted-foreground"}`}
            onClick={() => { setMode("file"); reset(); }}
          >
            <FileUp className="h-3.5 w-3.5" /> SQL file
          </button>
        </div>

        {!pending && (
          <label className="flex items-center justify-between gap-2 text-2xs font-medium text-muted-foreground">
            Database
            <Select
              value={driver}
              onChange={(e) => { setDriver(e.target.value); reset(); }}
              aria-label="Database type"
              className="w-40"
            >
              <option value="postgres">PostgreSQL</option>
              <option value="mysql">MySQL / MariaDB</option>
            </Select>
          </label>
        )}

        {pending ? (
          <ConfirmInference
            result={pending}
            busy={busy}
            onBack={() => setPending(null)}
            onConfirm={() => void loadResult(pending)}
          />
        ) : mode === "connect" ? (
          <div className="space-y-2">
            <p className="text-2xs text-muted-foreground">
              Connect to a {driver === "mysql" ? "MySQL/MariaDB" : "Postgres"} database. The engine
              introspects it into a map (a uuid foreign key stays uuid — on MySQL a CHAR(36) key is
              recognised as uuid). The connection string is used once and never stored.
            </p>

            {savedConnections.length > 0 && (
              <div className="space-y-1">
                <p className="text-2xs font-medium text-muted-foreground">Saved connections</p>
                <ul className="space-y-1">
                  {savedConnections.map((c) => (
                    <li key={c.id} className="flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => loadConnection(c)}
                        className="flex-1 truncate rounded border border-border px-2 py-1 text-left text-2xs hover:bg-muted"
                        title="Load this connection (you'll re-enter the password)"
                      >
                        <span className="font-medium text-foreground">{c.label || `${c.user}@${c.host}`}</span>
                        <span className="text-muted-foreground"> · {c.driver}</span>
                      </button>
                      <button
                        type="button"
                        aria-label={`Delete saved connection ${c.label}`}
                        onClick={() => removeConnection(c.id)}
                        className="rounded p-1 text-muted-foreground hover:text-destructive"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
                <p className="text-2xs text-muted-foreground">Passwords are never saved — re-enter the password to connect.</p>
              </div>
            )}
            <label className="block text-2xs font-medium text-muted-foreground">
              Connection string
              <Input
                value={dsn}
                onChange={(e) => setDsn(e.target.value)}
                placeholder={driver === "mysql" ? "mysql://user:pass@host:3306/dbname" : "postgresql://user:pass@host:5432/dbname"}
                aria-label="Connection string"
                className="mt-1 font-mono"
              />
            </label>
            <p className="rounded border border-amber-300/40 bg-amber-50/50 px-2 py-1.5 text-2xs text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400">
              <strong>Host tip:</strong> <code>localhost</code> means “this same machine”. The Designer runs
              inside a container, so for a database on <em>your computer</em> use{" "}
              <code>host.docker.internal</code>; for a remote server use its real IP/hostname.
              (<code>localhost</code> is rewritten automatically when possible.)
            </p>
            <p className="text-center text-2xs text-muted-foreground">— or enter the parts —</p>
            <div className="grid grid-cols-2 gap-2">
              <LabeledInput label="Host" value={host} onChange={setHost} placeholder="localhost" disabled={!!dsn.trim()} />
              <LabeledInput label="Port" value={port} onChange={setPort} placeholder="5432" disabled={!!dsn.trim()} />
              <LabeledInput label="Database" value={database} onChange={setDatabase} placeholder="app" disabled={!!dsn.trim()} />
              <LabeledInput label="User" value={user} onChange={setUser} placeholder="postgres" disabled={!!dsn.trim()} />
              <LabeledInput label="Password" value={password} onChange={setPassword} type="password" disabled={!!dsn.trim()} className="col-span-2" />
            </div>
            <div className="flex justify-end">
              <Button
                variant="ghost"
                size="sm"
                type="button"
                onClick={onSaveConnection}
                disabled={!!dsn.trim() || (!host.trim() && !database.trim())}
                className="gap-1 text-2xs"
                title="Save host/port/database/user for next time (the password is never saved)"
              >
                <BookmarkPlus className="h-3 w-3" /> Save connection
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <p className="text-2xs text-muted-foreground">
              Upload a SQL dump (a set of <code>CREATE TABLE</code> statements). The engine applies it to
              a temporary shadow database and introspects it — no SQL is parsed in the browser.
            </p>
            <label className="flex cursor-pointer flex-col items-center gap-2 rounded-md border border-dashed border-input px-4 py-6 text-center text-2xs text-muted-foreground hover:border-ring">
              <FileUp className="h-5 w-5" />
              {fileName ? <span className="font-medium text-foreground">{fileName}</span> : "Choose a .sql file"}
              <input
                type="file"
                accept=".sql,.ddl,.txt"
                className="hidden"
                onChange={(e) => void onPickFile(e.target.files?.[0])}
                aria-label="SQL file"
              />
            </label>
            {sql && (
              <pre className="max-h-24 overflow-auto rounded border border-border bg-muted/40 p-2 font-mono text-2xs text-muted-foreground">
                {sql.slice(0, 600)}{sql.length > 600 ? "\n…" : ""}
              </pre>
            )}
          </div>
        )}

        {error && <p className="text-2xs text-destructive">{error}</p>}

        {!pending && (
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={close} disabled={busy}>Cancel</Button>
            <Button size="sm" onClick={runImport} disabled={!canImport || busy} className="gap-1.5">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
              {busy ? "Importing…" : "Import"}
            </Button>
          </div>
        )}
      </div>
    </Dialog>
  );
}

function LabeledInput({
  label, value, onChange, placeholder, type, disabled, className,
}: {
  label: string; value: string; onChange: (v: string) => void;
  placeholder?: string; type?: string; disabled?: boolean; className?: string;
}) {
  return (
    <label className={`block text-2xs font-medium text-muted-foreground ${className ?? ""}`}>
      {label}
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        type={type}
        disabled={disabled}
        aria-label={label}
        className="mt-1"
      />
    </label>
  );
}

// AD-5: the engine flags low-confidence reverse-inferences; the human confirms before they're used.
// We don't auto-apply anything — the deterministic best guess is already in the schema, and the user
// can refine these types in the details panel after loading. This step just makes them visible.
function ConfirmInference({
  result, busy, onBack, onConfirm,
}: { result: ImportResult; busy: boolean; onBack: () => void; onConfirm: () => void }) {
  const suggestions = result.inference?.suggestions ?? [];
  return (
    <div className="space-y-2">
      <p className="text-2xs text-muted-foreground">
        Imported. The engine inferred {result.inference?.confident ?? 0} types confidently and flagged
        these {suggestions.length} for you to confirm — review and adjust them on the canvas after loading.
      </p>
      <ul className="max-h-56 space-y-1 overflow-y-auto">
        {suggestions.map((s: ImportSuggestion, i) => (
          <li key={i} className="rounded border border-border/70 px-2 py-1.5 text-2xs">
            <span className="font-mono font-medium text-foreground">
              {s.column ? `${s.table}.${s.column}` : s.relation}
            </span>
            <span className="text-muted-foreground">
              {s.physicalType ? ` · ${s.physicalType}` : ""} → {s.llmSuggestion ?? s.suggestedType}
              {s.confidence != null ? ` (${Math.round(s.confidence * 100)}%)` : ""}
            </span>
          </li>
        ))}
      </ul>
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="outline" size="sm" onClick={onBack} disabled={busy}>Back</Button>
        <Button size="sm" onClick={onConfirm} disabled={busy}>Load into canvas</Button>
      </div>
    </div>
  );
}
