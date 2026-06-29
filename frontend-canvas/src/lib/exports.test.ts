import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Export plumbing (export-formats + close-debts milestones): the text-format Download helper and the
// vector ERD export. The ERD is now rendered straight from the schema data (see erdExport.test.ts for
// the renderer itself); here we only test the browser download wiring and the engine code-gen call.

import { exportErd } from "./erdExport";
import { downloadText } from "./download";
import { generateCode } from "./api";
import type { Positions } from "./layout";
import { RENDER_MODEL } from "@/test/fixtures";
import type { RenderModel } from "@/lib/types";

const POSITIONS: Positions = {
  tbl_users0001: { x: 0, y: 0 },
  tbl_orders001: { x: 420, y: 240 },
};

let clickSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  globalThis.URL.createObjectURL = vi.fn(() => "blob:erd");
  globalThis.URL.revokeObjectURL = vi.fn();
});
afterEach(() => vi.clearAllMocks());

describe("downloadText", () => {
  it("builds a blob and triggers a download", () => {
    downloadText("schema.yaml", "database: shop\n", "text/yaml");
    expect(globalThis.URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(globalThis.URL.revokeObjectURL).toHaveBeenCalledWith("blob:erd");
  });
});

describe("exportErd (vector render from schema data)", () => {
  it("SVG export downloads a vector file built from the model (no DOM screenshot)", async () => {
    await exportErd("svg", RENDER_MODEL, POSITIONS, { name: "shop" });
    // SVG goes out through the text-download path (a real <svg> string, not a canvas capture).
    expect(globalThis.URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("refuses to export an empty canvas", async () => {
    const empty: RenderModel = { ...RENDER_MODEL, tables: [], relations: [] };
    await expect(exportErd("png", empty, {}, {})).rejects.toThrow(/add some tables/i);
  });
});

describe("generateCode wiring for the new text artifacts", () => {
  afterEach(() => vi.restoreAllMocks());
  it("posts the selected kind to /design/code", async () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ content: "Table users {}", kind: "dbml" }), { status: 200 }));
    globalThis.fetch = fetchMock as never;
    const out = await generateCode({ schemaJson: { formatVersion: "1.0.0", logical: { tables: [] } }, kind: "dbml" });
    expect(out).toContain("Table users");
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.kind).toBe("dbml");
  });
});
