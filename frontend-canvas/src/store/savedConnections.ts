import { create } from "zustand";

// Saved database connections (import-fixes milestone §3) — a convenience so a user doesn't retype
// host/port/db/user every time.
//
// SECURITY (the heart of this feature): the **password is never stored**. We persist only the
// non-secret connection parts to localStorage; the password is re-entered each time a connection is
// used. This is the spec's safest option — no plaintext secret in the browser, in localStorage, or in
// any log — chosen because secure password storage would need server-side encryption + key management.

export interface SavedConnection {
  id: string;
  label: string;
  driver: string;
  host: string;
  port: string;
  database: string;
  user: string;
  // password is intentionally absent — see the module note above.
}

const KEY = "vdb.connections.v1";

/** Keep only the non-secret fields — defensively strips any password-like key that slipped in. */
function sanitize(c: Partial<SavedConnection> & Record<string, unknown>): SavedConnection | null {
  if (!c || typeof c !== "object" || !c.id) return null;
  return {
    id: String(c.id),
    label: String(c.label ?? ""),
    driver: String(c.driver ?? "postgres"),
    host: String(c.host ?? ""),
    port: String(c.port ?? ""),
    database: String(c.database ?? ""),
    user: String(c.user ?? ""),
  };
}

function read(): SavedConnection[] {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(KEY) : null;
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed.map(sanitize).filter(Boolean) as SavedConnection[]) : [];
  } catch {
    return [];
  }
}

function persist(list: SavedConnection[]): void {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* localStorage may be unavailable/full — saving is best-effort, never fatal */
  }
}

let seq = 0;
function newId(): string {
  seq += 1;
  const stamp = typeof Date !== "undefined" ? Date.now().toString(36) : "x";
  return `conn_${stamp}_${seq}`;
}

interface SavedConnectionsState {
  connections: SavedConnection[];
  /** Save (or update) a connection. Any `password` on the input is dropped by `sanitize`. */
  save: (conn: Partial<SavedConnection> & Record<string, unknown>) => SavedConnection | null;
  remove: (id: string) => void;
  reload: () => void;
}

export const useSavedConnections = create<SavedConnectionsState>((set, get) => ({
  connections: read(),
  save: (conn) => {
    const entry = sanitize({ ...conn, id: conn.id ?? newId() });
    if (!entry) return null;
    const next = [...get().connections.filter((c) => c.id !== entry.id), entry];
    persist(next);
    set({ connections: next });
    return entry;
  },
  remove: (id) => {
    const next = get().connections.filter((c) => c.id !== id);
    persist(next);
    set({ connections: next });
  },
  reload: () => set({ connections: read() }),
}));
