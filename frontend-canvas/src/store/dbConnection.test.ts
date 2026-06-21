import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { driftColors, driftIsEmpty, useCanvasStore } from "./canvasStore";
import { importLiveDatabase, importSqlDump } from "@/lib/api";
import { RENDER_MODEL } from "@/test/fixtures";
import type { SchemaDoc } from "@/lib/schema";
import type { DriftReport, RenderModel } from "@/lib/types";

// Database-connection milestone: import (live + file) and drift are engine endpoints; the front-end
// only surfaces them. These tests pin the request shapes and the read-only drift reflection — no
// database logic is decided in the browser (golden rule).

const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        { id: "tbl_users0001", name: "users", fields: [
          { id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
        ] },
      ],
      relations: [],
    },
  };
}

function routedFetch() {
  return vi.fn(async (url: RequestInfo | URL) => {
    const u = String(url);
    if (u.includes("/design/render")) return json(RENDER_MODEL);
    if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
    if (u.includes("/core/diff")) return json({ operations: [] });
    if (u.includes("/core/types")) return json({ types: [] });
    return json({});
  });
}

describe("import API request shapes (engine-backed, no SQL parsed in the browser)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("live import posts a dsn to /design/import", async () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      json({ schema_json: doc(), inference: { confident: 1, ambiguous: 0, suggestions: [] } }));
    globalThis.fetch = fetchMock as never;
    await importLiveDatabase("postgresql://u@h:5432/db");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/design/import");
    expect(JSON.parse(String(init?.body))).toEqual({ dsn: "postgresql://u@h:5432/db" });
  });

  it("file import posts the SQL text (the engine applies it to a shadow DB)", async () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      json({ schema_json: doc(), inference: { confident: 1, ambiguous: 0, suggestions: [] } }));
    globalThis.fetch = fetchMock as never;
    await importSqlDump("CREATE TABLE users (id uuid PRIMARY KEY);");
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.sql).toContain("CREATE TABLE");
    expect(body.dsn).toBeUndefined();
  });
});

describe("compareWithDatabase (drift) action", () => {
  beforeEach(async () => {
    globalThis.fetch = routedFetch() as never;
    await useCanvasStore.getState().load({ schemaJson: doc() });
  });
  afterEach(() => vi.restoreAllMocks());

  it("stores the engine's drift report and sends the designed schema + live dsn", async () => {
    const report: DriftReport = {
      reconcile: { matched: 1, ambiguous: [] },
      drift: [{ entity: "users.phone", kind: "column", status: { designed: true }, category: "migration_not_applied", severity: "warning", detail: "x", suggestion: null }],
      summary: { migration_not_applied: 1 },
      exitCode: 0,
    };
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/design/drift")) {
        const body = JSON.parse(String(init?.body));
        expect(body.liveDsn).toBe("postgresql://live");
        expect(body.designed.logical.tables[0].name).toBe("users");
        return json(report);
      }
      return json({});
    });
    globalThis.fetch = fetchMock as never;

    const res = await useCanvasStore.getState().compareWithDatabase("postgresql://live");
    expect(res.ok).toBe(true);
    expect(useCanvasStore.getState().drift?.drift).toHaveLength(1);
  });

  it("an edit clears a stale drift report", async () => {
    useCanvasStore.setState({ drift: { reconcile: { matched: 0, ambiguous: [] }, drift: [], summary: {}, exitCode: 0 } });
    await useCanvasStore.getState().addTable("widgets");
    expect(useCanvasStore.getState().drift).toBeNull();
  });
});

describe("driftColors reflects only design-present drift onto the canvas", () => {
  const model: RenderModel = RENDER_MODEL;

  it("tints design-only/not-applied green and type drift yellow; skips db-only entities", () => {
    const report: DriftReport = {
      reconcile: { matched: 2, ambiguous: [] },
      drift: [
        // design-only table → green on the table
        { entity: "drafts", kind: "table", status: { designed: true }, category: "design_ahead_of_code", severity: "warning", detail: null, suggestion: null },
        // type differs on orders.user_id → yellow on the field
        { entity: "orders.user_id", kind: "type", status: { designed: true, migrations: true, live: true }, category: "migration_incomplete", severity: "error", detail: null, suggestion: null },
        // live-only column (manual prod change) → NOT on the canvas, no tint
        { entity: "users.hotfix", kind: "column", status: { live: true }, category: "manual_prod_change", severity: "warning", detail: null, suggestion: null },
      ],
      summary: {},
      exitCode: 1,
    };
    const colors = driftColors(report, model);
    expect(colors.fields.get("fld_ouser")).toBe("yellow"); // orders.user_id
    expect(colors.tables.get("tbl_orders001")).toBe("yellow"); // its table is marked changed
    // "drafts" isn't a table in the model, so nothing to tint there; "users.hotfix" is skipped.
    expect([...colors.fields.keys()]).toEqual(["fld_ouser"]);
  });

  it("driftIsEmpty is true for a null or empty report", () => {
    expect(driftIsEmpty(null)).toBe(true);
    expect(driftIsEmpty({ reconcile: { matched: 0, ambiguous: [] }, drift: [], summary: {}, exitCode: 0 })).toBe(true);
  });
});
