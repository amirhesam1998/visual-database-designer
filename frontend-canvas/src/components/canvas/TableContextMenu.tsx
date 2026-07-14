import { useEffect, useRef, useState, type ReactNode } from "react";
import { Copy, Pencil, Trash2 } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { relationsReferencing, tableById as docTableById } from "@/lib/schema";
import { useCanvasStore } from "@/store/canvasStore";

// Right-click actions on a table node (B1): rename / duplicate / delete. Every action goes through the
// existing store mutations (which round-trip to the engine + are undoable) — this component decides
// nothing structural. Delete is CASCADING (edit.removeTable already drops dangling relations/indexes),
// so when a table is referenced we confirm first and tell the user exactly how many relationships go
// with it: cascade, but never silent (spec §0 / B1 "cascade-handle or block-and-warn — decide and
// document"; decision = cascade-with-confirmation).

type Mode = "menu" | "rename" | "confirm-delete";

export interface TableMenuTarget {
  tableId: string;
  x: number;
  y: number;
}

function MenuItem({
  icon,
  onClick,
  destructive,
  children,
}: {
  icon: ReactNode;
  onClick: () => void;
  destructive?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-accent",
        destructive && "text-destructive",
      )}
    >
      {icon}
      {children}
    </button>
  );
}

export function TableContextMenu({ tableId, x, y, onClose }: TableMenuTarget & { onClose: () => void }) {
  const doc = useCanvasStore((s) => s.doc);
  const updateTable = useCanvasStore((s) => s.updateTable);
  const duplicateTable = useCanvasStore((s) => s.duplicateTable);
  const removeTable = useCanvasStore((s) => s.removeTable);

  const table = doc ? docTableById(doc, tableId) : undefined;
  const [mode, setMode] = useState<Mode>("menu");
  const [name, setName] = useState(table?.name ?? "");
  const menuRef = useRef<HTMLDivElement>(null);

  // Dismiss the bare menu on Escape or an outside click. A modal dialog (rename/confirm) manages its
  // own dismissal, so only bind while the plain menu is showing.
  useEffect(() => {
    if (mode !== "menu") return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [mode, onClose]);

  if (!table) return null;

  const refs = doc ? relationsReferencing(doc, tableId) : [];

  const doDelete = () => {
    void removeTable(tableId);
    onClose();
  };

  if (mode === "rename") {
    const commit = () => {
      const trimmed = name.trim();
      if (trimmed && trimmed !== table.name) void updateTable(tableId, { name: trimmed });
      onClose();
    };
    return (
      <Dialog open onClose={onClose} title="Rename table">
        <div className="space-y-3 text-xs">
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && commit()}
            aria-label="Table name"
            className="h-8 text-xs"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" onClick={commit} disabled={!name.trim()}>
              Rename
            </Button>
          </div>
        </div>
      </Dialog>
    );
  }

  if (mode === "confirm-delete") {
    return (
      <Dialog open onClose={onClose} title={`Delete ${table.name}?`}>
        <div className="space-y-3 text-xs">
          <p className="text-muted-foreground">
            This permanently removes <span className="font-medium text-foreground">{table.name}</span> and
            cascade-deletes{" "}
            <span className="font-medium text-foreground">{refs.length}</span> relationship
            {refs.length === 1 ? "" : "s"} that reference it. You can undo this.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={doDelete}
              className="bg-destructive text-destructive-foreground hover:opacity-90"
            >
              Delete
            </Button>
          </div>
        </div>
      </Dialog>
    );
  }

  return (
    <div
      ref={menuRef}
      role="menu"
      style={{ left: x, top: y }}
      className="fixed z-50 min-w-[168px] rounded-md border border-border bg-card py-1 text-xs shadow-xl"
      data-testid={`table-context-menu-${table.name}`}
    >
      <MenuItem
        icon={<Pencil className="h-3.5 w-3.5" />}
        onClick={() => {
          setName(table.name);
          setMode("rename");
        }}
      >
        Rename
      </MenuItem>
      <MenuItem icon={<Copy className="h-3.5 w-3.5" />} onClick={() => {
        void duplicateTable(tableId);
        onClose();
      }}>
        Duplicate
      </MenuItem>
      <MenuItem
        icon={<Trash2 className="h-3.5 w-3.5" />}
        destructive
        onClick={() => (refs.length > 0 ? setMode("confirm-delete") : doDelete())}
      >
        Delete{refs.length > 0 ? "…" : ""}
      </MenuItem>
    </div>
  );
}
