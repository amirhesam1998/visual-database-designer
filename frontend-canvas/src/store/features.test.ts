import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanvasStore } from "./canvasStore";
import type { SchemaDoc } from "@/lib/schema";
import { RENDER_MODEL } from "@/test/fixtures";

// Phase-2 feature actions (duplicate, timestamps, enums, indexes, generate-from-PRD). Each is the
// engine-connected path: a structural mutation followed by the render/validate round-trip — no
// database logic decided in the front-end (unify spec phase 2 §0/§2).

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

const GENERATED: SchemaDoc = {
  formatVersion: "1.0.0",
  logical: { tables: [{ id: "tbl_new000001", name: "widgets", fields: [
    { id: "fld_new000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
  ] }], relations: [] },
};

function routedFetch() {
  return vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/design/render")) return json(RENDER_MODEL);
    if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
    if (u.includes("/core/diff")) return json({ operations: [] });
    if (u.includes("/core/types")) return json({ types: [] });
    if (u.includes("/design/sessions") && u.endsWith("/suggest")) return json({ suggestion: GENERATED });
    if (u.includes("/design/sessions") && u.endsWith("/apply-suggestion")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return json({ state: "draft", schema_json: body.schema_json });
    }
    if (u.includes("/design/sessions")) return json({ sessionId: "sess_1" });
    return json({});
  });
}

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        { id: "tbl_users0001", name: "users", fields: [
          { id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_email0001", name: "email", semanticType: "email", nullable: false },
        ] },
      ],
      relations: [],
    },
  };
}

const users = (d: SchemaDoc) => d.logical.tables.find((t) => t.id === "tbl_users0001")!;

describe("phase-2 feature store actions", () => {
  beforeEach(async () => {
    globalThis.fetch = routedFetch() as never;
    await useCanvasStore.getState().load({ schemaJson: doc() });
  });
  afterEach(() => vi.restoreAllMocks());

  it("duplicateTable adds a copy with fresh ids and selects it", async () => {
    await useCanvasStore.getState().duplicateTable("tbl_users0001");
    const s = useCanvasStore.getState();
    expect(s.doc!.logical.tables).toHaveLength(2);
    const copy = s.doc!.logical.tables.find((t) => t.name === "users_copy")!;
    expect(copy.id).not.toBe("tbl_users0001");
    expect(s.selectedTableId).toBe(copy.id);
  });

  it("setTimestamps adds real datetime columns through the engine round-trip", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();
    await useCanvasStore.getState().setTimestamps("tbl_users0001", true);
    const names = users(useCanvasStore.getState().doc!).fields.map((f) => f.name);
    expect(names).toContain("created_at");
    expect(names).toContain("updated_at");
    const called = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(called.some((u) => u.includes("/core/validate"))).toBe(true);
  });

  it("enum + index actions edit the engine layers", async () => {
    await useCanvasStore.getState().addEnum("status", ["a", "b"]);
    expect(useCanvasStore.getState().doc!.logical.enums).toHaveLength(1);
    await useCanvasStore.getState().addIndex("tbl_users0001", { columns: ["fld_email0001"], unique: true });
    expect(useCanvasStore.getState().doc!.physical!.indexes).toHaveLength(1);
  });

  it("replaceDoc swaps in an engine-generated schema and resets the diff base", async () => {
    await useCanvasStore.getState().replaceDoc(GENERATED);
    const s = useCanvasStore.getState();
    expect(s.doc!.logical.tables[0].name).toBe("widgets");
    expect(s.baseline).toBe(GENERATED); // generated schema is the new base, not a delta
  });
});
