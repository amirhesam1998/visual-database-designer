import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanvasStore } from "./canvasStore";
import type { SchemaDoc } from "@/lib/schema";
import { RENDER_MODEL } from "@/test/fixtures";

// Bug §5 — "search/create doesn't jump to the table in a large map". The viewport jump itself is
// React Flow geometry (Canvas.tsx computes a bounding box and calls `fitBounds`, which works even for
// an off-screen, never-measured node under `onlyRenderVisibleElements`). What the store owns — and
// what these tests pin — is the *trigger*: creating/duplicating/focusing a table selects it AND bumps
// a monotonic `focusNonce` the canvas watches, so the jump fires regardless of how many tables exist.

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

function routedFetch() {
  return vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/design/render")) return json(RENDER_MODEL);
    if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
    if (u.includes("/core/types")) return json({ types: [{ id: "string", category: "string", physical: {}, pii: false }] });
    if (u.includes("/design/presentation")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return json({ schema_json: body.schema_json, persisted: false });
    }
    return json({});
  });
}

/** A schema with `n` tables — used to prove the jump trigger fires at large scale (100+ tables), the
 *  exact case where React Flow virtualisation broke the old `fitView`-based jump. */
function bigDoc(n: number): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: Array.from({ length: n }, (_, i) => ({
        id: `tbl_${String(i).padStart(8, "0")}`,
        name: `t${i}`,
        fields: [{ id: `fld_${String(i).padStart(8, "0")}`, name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }],
      })),
      relations: [],
    },
  };
}

const tableIds = (d: SchemaDoc) => d.logical.tables.map((t) => t.id);

describe("canvasStore focus/jump trigger (bug §5)", () => {
  beforeEach(() => {
    globalThis.fetch = routedFetch() as never;
  });
  afterEach(() => vi.restoreAllMocks());

  it("focusTable selects the table AND bumps focusNonce (the canvas jump signal)", () => {
    const before = useCanvasStore.getState().focusNonce;
    useCanvasStore.getState().focusTable("tbl_00000042");
    const s = useCanvasStore.getState();
    expect(s.selectedTableId).toBe("tbl_00000042");
    expect(s.focusNonce).toBe(before + 1);
  });

  it("re-focusing the same table still bumps the nonce (so a repeated jump fires)", () => {
    useCanvasStore.getState().focusTable("tbl_00000001");
    const mid = useCanvasStore.getState().focusNonce;
    useCanvasStore.getState().focusTable("tbl_00000001");
    expect(useCanvasStore.getState().focusNonce).toBe(mid + 1);
  });

  it("adding a table in a 120-table map focuses the new table so the canvas jumps to it", async () => {
    await useCanvasStore.getState().load({ schemaJson: bigDoc(120) });
    const idsBefore = new Set(tableIds(useCanvasStore.getState().doc!));
    const nonceBefore = useCanvasStore.getState().focusNonce;

    await useCanvasStore.getState().addTable("brand_new_table");

    const s = useCanvasStore.getState();
    const created = tableIds(s.doc!).find((id) => !idsBefore.has(id))!;
    expect(created).toBeDefined();                 // the table was really added
    expect(s.doc!.logical.tables).toHaveLength(121);
    expect(s.selectedTableId).toBe(created);        // …and it is the selected/focused one
    expect(s.focusNonce).toBe(nonceBefore + 1);     // …with the jump signal fired (works at any scale)
  });

  it("duplicating a table focuses the copy (brings it into view, not left off-screen)", async () => {
    await useCanvasStore.getState().load({ schemaJson: bigDoc(120) });
    const idsBefore = new Set(tableIds(useCanvasStore.getState().doc!));
    const nonceBefore = useCanvasStore.getState().focusNonce;

    await useCanvasStore.getState().duplicateTable("tbl_00000005");

    const s = useCanvasStore.getState();
    const copy = tableIds(s.doc!).find((id) => !idsBefore.has(id))!;
    expect(s.selectedTableId).toBe(copy);
    expect(s.focusNonce).toBe(nonceBefore + 1);
  });
});
