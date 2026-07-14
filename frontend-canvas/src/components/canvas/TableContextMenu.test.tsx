import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TableContextMenu } from "./TableContextMenu";
import { useCanvasStore } from "@/store/canvasStore";
import type { SchemaDoc } from "@/lib/schema";

// B1: the table context menu drives the existing store mutations (rename / duplicate / delete). Delete
// is cascading, so a referenced table must confirm first and state how many relationships it takes
// with it; an unreferenced table deletes immediately. Every action goes through the engine round-trip.

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        { id: "tbl_users0001", name: "users", fields: [{ id: "fld_uid", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }] },
        {
          id: "tbl_orders001",
          name: "orders",
          fields: [
            { id: "fld_oid", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
            { id: "fld_ouser", name: "user_id", semanticType: "foreign_key", nullable: false },
          ],
        },
        { id: "tbl_tags00001", name: "tags", fields: [{ id: "fld_tid", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }] },
      ],
      relations: [
        { id: "rel_ord_usr", type: "one_to_many", fromTableId: "tbl_orders001", toTableId: "tbl_users0001", foreignKeyFieldId: "fld_ouser" },
      ],
    },
  };
}

const tables = () => useCanvasStore.getState().doc!.logical.tables;
const tableNames = () => tables().map((t) => t.name);

describe("TableContextMenu (B1 — rename / duplicate / delete)", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
      return json({ tables: [], relations: [] }); // /design/render — content irrelevant to these tests
    }) as never;
    useCanvasStore.setState({ doc: doc(), model: null, editable: true, past: [], future: [], savedSignature: "" });
  });
  afterEach(() => vi.restoreAllMocks());

  const open = (tableId: string, onClose = vi.fn()) => {
    render(<TableContextMenu tableId={tableId} x={20} y={20} onClose={onClose} />);
    return onClose;
  };

  it("renders the three actions for a table", () => {
    open("tbl_tags00001");
    expect(screen.getByRole("menuitem", { name: /rename/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /duplicate/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /delete/i })).toBeInTheDocument();
  });

  it("Duplicate adds a copy through the store and closes the menu", async () => {
    const onClose = open("tbl_users0001");
    fireEvent.click(screen.getByRole("menuitem", { name: /duplicate/i }));
    expect(onClose).toHaveBeenCalled();
    await waitFor(() => expect(tables()).toHaveLength(4));
    expect(tableNames()).toContain("users_copy");
  });

  it("deletes an UNreferenced table immediately (no confirmation)", async () => {
    const onClose = open("tbl_tags00001");
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // nothing references tags → no warning
    expect(onClose).toHaveBeenCalled();
    await waitFor(() => expect(tableNames()).not.toContain("tags"));
  });

  it("WARNS before deleting a referenced table, then cascades on confirm (B1 decision)", async () => {
    open("tbl_users0001"); // referenced by orders.user_id
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }));

    // A confirmation dialog appears and states the cascade count.
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(/Delete users\?/i);
    expect(dialog).toHaveTextContent(/1 relationship/i);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(tableNames()).not.toContain("users");
      expect(useCanvasStore.getState().doc!.logical.relations).toHaveLength(0); // relation cascaded
    });
  });

  it("Rename mutates the existing table id's name (never a new id)", async () => {
    open("tbl_users0001");
    fireEvent.click(screen.getByRole("menuitem", { name: /rename/i }));
    fireEvent.change(screen.getByLabelText(/table name/i), { target: { value: "accounts" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => {
      const t = tables().find((x) => x.id === "tbl_users0001")!;
      expect(t.name).toBe("accounts"); // same id, new name (AD-1)
    });
  });
});
