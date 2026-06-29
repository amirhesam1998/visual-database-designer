import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { insightCounts, insightsByEntity, useCanvasStore } from "./canvasStore";
import { RENDER_MODEL } from "@/test/fixtures";
import type { SchemaDoc } from "@/lib/schema";
import type { Insight } from "@/lib/types";

// Intelligence milestone: the engine analyses the schema and returns insights; the canvas only shows
// them and, on demand, applies a finding's action as a NORMAL structural edit (spec §5). These tests
// pin the pure selectors and that applying an insight produces the same edit the user could make.

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        { id: "tbl_users0001", name: "users", fields: [
          { id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_uphone001", name: "phone_number", semanticType: "string", nullable: true },
        ] },
        { id: "tbl_orders001", name: "orders", fields: [
          { id: "fld_oid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_ouser0001", name: "user_id", semanticType: "foreign_key", nullable: false },
        ] },
      ],
      relations: [],
    },
  };
}

function insight(over: Partial<Insight>): Insight {
  return {
    rule_id: "X", category: "design", kind: "suggestion", severity: "info",
    title: "t", why: "w", path: "", entity_id: null, table_id: null, field_id: null,
    fix: null, action: null, ...over,
  };
}

function routedFetch() {
  return vi.fn(async (url: RequestInfo | URL) => {
    const u = String(url);
    if (u.includes("/design/render")) return json(RENDER_MODEL);
    if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
    if (u.includes("/core/diff")) return json({ operations: [] });
    if (u.includes("/design/insights")) return json({ insights: [], summary: {} });
    if (u.includes("/core/types")) return json({ types: [] });
    return json({});
  });
}

describe("insight selectors (pure projections of the engine list)", () => {
  it("insightCounts splits facts from suggestions and tallies actionable ones", () => {
    const list = [
      insight({ kind: "fact" }),
      insight({ kind: "suggestion", action: { type: "add_index", label: "Add index" } }),
      insight({ kind: "suggestion" }),
    ];
    expect(insightCounts(list)).toEqual({ facts: 1, suggestions: 2, actionable: 1 });
  });

  it("insightsByEntity groups by the entity id and drops schema-level findings", () => {
    const list = [
      insight({ rule_id: "A", entity_id: "fld_1" }),
      insight({ rule_id: "B", entity_id: "fld_1" }),
      insight({ rule_id: "C", entity_id: null }), // schema-level (e.g. naming) — excluded
    ];
    const map = insightsByEntity(list);
    expect(map.get("fld_1")?.map((i) => i.rule_id)).toEqual(["A", "B"]);
    expect(map.has("")).toBe(false);
  });
});

describe("applyInsight performs the SAME structural edit a user could make", () => {
  beforeEach(async () => {
    globalThis.fetch = routedFetch() as never;
    await useCanvasStore.getState().load({ schemaJson: doc() });
  });
  afterEach(() => vi.restoreAllMocks());

  it("add_index appends an index on the engine's physical.indexes", async () => {
    await useCanvasStore.getState().applyInsight(
      insight({ rule_id: "IDX001", action: { type: "add_index", label: "Add index",
        table_id: "tbl_orders001", columns: ["fld_ouser0001"], unique: false } }),
    );
    const indexes = useCanvasStore.getState().doc?.physical?.indexes ?? [];
    expect(indexes).toHaveLength(1);
    expect(indexes[0]).toMatchObject({ tableId: "tbl_orders001", columns: ["fld_ouser0001"], unique: false });
  });

  it("mark_sensitive sets a pii privacy override the Type System reads", async () => {
    await useCanvasStore.getState().applyInsight(
      insight({ rule_id: "PRV002", action: { type: "mark_sensitive", label: "Mark sensitive",
        table_id: "tbl_users0001", field_id: "fld_uphone001", sensitivity: "medium" } }),
    );
    const field = useCanvasStore.getState().doc?.logical.tables[0].fields
      .find((f) => f.id === "fld_uphone001");
    expect((field?.overrides as { privacy?: { pii?: boolean; sensitivity?: string } })?.privacy)
      .toEqual({ pii: true, sensitivity: "medium" });
  });

  it("an action-less (fact) insight is a no-op", async () => {
    await useCanvasStore.getState().applyInsight(insight({ rule_id: "DSN001", action: null }));
    expect(useCanvasStore.getState().doc?.physical?.indexes ?? []).toHaveLength(0);
  });
});
