import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { DetailsPanel } from "./DetailsPanel";
import { useCanvasStore } from "@/store/canvasStore";
import { RENDER_MODEL } from "@/test/fixtures";
import type { SchemaDoc } from "@/lib/schema";

// The editable side panel (spec §1): a field can be added/typed from the form, every change is
// validated by the engine, and validation findings appear inline next to the entity (§6).

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        {
          id: "tbl_users0001",
          name: "users",
          fields: [
            { id: "fld_uid", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
            { id: "fld_email", name: "email", semanticType: "email", nullable: false },
          ],
        },
        { id: "tbl_orders001", name: "orders", fields: [{ id: "fld_oid", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }] },
      ],
      relations: [],
    },
  };
}

describe("DetailsPanel (editable)", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
      return json(RENDER_MODEL);
    }) as never;
    useCanvasStore.setState({
      model: RENDER_MODEL,
      doc: doc(),
      savedSignature: "",
      past: [],
      future: [],
      editable: true,
      selectedTableId: "tbl_users0001",
      validation: null,
      types: [],
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("adds a field from the form and re-validates via the engine (spec §1/§7)", async () => {
    render(<DetailsPanel />);
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();

    fireEvent.click(screen.getByLabelText(/add field/i));

    // the document gained a field…
    const users = useCanvasStore.getState().doc!.logical.tables.find((t) => t.id === "tbl_users0001")!;
    expect(users.fields).toHaveLength(3);
    // …and the engine was asked to re-render + re-validate it.
    await waitFor(() => {
      const called = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
      expect(called.some((u) => u.includes("/design/render"))).toBe(true);
      expect(called.some((u) => u.includes("/core/validate"))).toBe(true);
    });
  });

  it("offers a semantic-type dropdown for an editable field", () => {
    render(<DetailsPanel />);
    expect(screen.getByLabelText(/type for email/i)).toBeInTheDocument();
  });

  it("shows an engine validation finding inline next to its field (spec §6)", () => {
    useCanvasStore.setState({
      validation: {
        valid: false,
        summary: {},
        findings: [
          {
            rule_id: "QLT010",
            severity: "warning",
            message: "Email field users.email is not covered by a unique index.",
            path: "users.email",
            entity_id: "fld_email",
            fix: "Add a unique index on this column.",
          },
        ],
      },
    });
    render(<DetailsPanel />);
    const editor = screen.getByTestId("field-editor-email");
    expect(within(editor).getByText(/not covered by a unique index/i)).toBeInTheDocument();
  });
});
