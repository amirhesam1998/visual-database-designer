import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isDirty, useCanvasStore } from "./canvasStore";
import type { SchemaDoc } from "@/lib/schema";
import { RENDER_MODEL } from "@/test/fixtures";

// The editing store is the proof of the spec's central rule (§0): the front-end only mutates the
// document structurally; every edit is followed by an engine round-trip (/design/render +
// /core/validate), and a relation can never be created without its foreign-key field (§2/§3).

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

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        {
          id: "tbl_users0001",
          name: "users",
          fields: [{ id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }],
        },
        {
          id: "tbl_orders001",
          name: "orders",
          fields: [{ id: "fld_oid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }],
        },
      ],
      relations: [],
    },
  };
}

const usersTable = (d: SchemaDoc) => d.logical.tables.find((t) => t.id === "tbl_users0001")!;
const ordersTable = (d: SchemaDoc) => d.logical.tables.find((t) => t.id === "tbl_orders001")!;

describe("canvasStore editing (engine-validated)", () => {
  beforeEach(async () => {
    globalThis.fetch = routedFetch() as never;
    await useCanvasStore.getState().load({ schemaJson: doc() });
  });
  afterEach(() => vi.restoreAllMocks());

  it("loads the document + engine-resolved model and starts clean (no unsaved changes)", () => {
    const s = useCanvasStore.getState();
    expect(s.status).toBe("ready");
    expect(s.doc?.logical.tables).toHaveLength(2);
    expect(s.model).toEqual(RENDER_MODEL);
    expect(isDirty(s)).toBe(false);
  });

  it("addField mutates the document AND re-validates through the engine (spec §7)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();
    await useCanvasStore.getState().addField("tbl_users0001", { name: "email", semanticType: "email" });

    expect(usersTable(useCanvasStore.getState().doc!).fields.map((f) => f.name)).toEqual(["id", "email"]);
    const called = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(called.some((u) => u.includes("/design/render"))).toBe(true);
    expect(called.some((u) => u.includes("/core/validate"))).toBe(true);
    expect(isDirty(useCanvasStore.getState())).toBe(true);
  });

  it("connect creates a COMPLETE relation with a new foreign_key field (FK type left to the engine)", async () => {
    await useCanvasStore.getState().connect({
      fromTableId: "tbl_orders001",
      toTableId: "tbl_users0001",
      type: "one_to_many",
      newFkFieldName: "user_id",
    });

    const d = useCanvasStore.getState().doc!;
    const fk = ordersTable(d).fields.find((f) => f.name === "user_id")!;
    expect(fk).toBeDefined();
    expect(fk.semanticType).toBe("foreign_key"); // the front-end never sets the physical type
    expect(d.logical.relations).toHaveLength(1);
    expect(d.logical.relations![0].foreignKeyFieldId).toBe(fk.id); // never incomplete
  });

  it("refuses to create an incomplete relation — connect without any FK field is a no-op (spec §2/§3)", async () => {
    await useCanvasStore.getState().connect({
      fromTableId: "tbl_orders001",
      toTableId: "tbl_users0001",
      type: "one_to_many",
      // no fkFieldId, no newFkFieldName
    });
    expect(useCanvasStore.getState().doc!.logical.relations).toHaveLength(0);
  });

  it("a table move saves to presentation only and is NOT an unsaved schema change (spec §4)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();
    const before = useCanvasStore.getState().savedSignature;
    useCanvasStore.getState().commitPositions({ tbl_users0001: { x: 500, y: 500 } });
    await Promise.resolve();

    const called = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(called.some((u) => u.includes("/design/presentation"))).toBe(true);
    const s = useCanvasStore.getState();
    expect(isDirty(s)).toBe(false); // layout never counts as a schema change
    expect(s.savedSignature).toBe(before);
  });

  it("undo reverts the last edit and clears the dirty flag again", async () => {
    await useCanvasStore.getState().addField("tbl_users0001", { name: "email", semanticType: "email" });
    expect(isDirty(useCanvasStore.getState())).toBe(true);

    await useCanvasStore.getState().undo();
    const s = useCanvasStore.getState();
    expect(usersTable(s.doc!).fields.map((f) => f.name)).toEqual(["id"]);
    expect(isDirty(s)).toBe(false);
  });
});
