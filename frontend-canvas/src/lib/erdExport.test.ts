import { describe, expect, it } from "vitest";
import { buildErdSvg, erdBounds, paginate } from "./erdExport";
import { NODE_WIDTH, type Positions } from "./layout";
import { RENDER_MODEL } from "@/test/fixtures";
import type { RenderModel } from "@/lib/types";

// The vector ERD renderer (close-debts milestone §1). These cover the *pure* core — the SVG built from
// the schema data + positions, and the PDF page tiling. Rasterisation (PNG/PDF pixels) is a thin
// browser wrapper over this and is verified visually (the spec's acceptance is by eye), not here.

const POSITIONS: Positions = {
  tbl_users0001: { x: 0, y: 0 },
  tbl_orders001: { x: 400, y: 120 },
};

describe("buildErdSvg", () => {
  it("renders an SVG straight from the schema data (no DOM screenshot)", () => {
    const svg = buildErdSvg(RENDER_MODEL, POSITIONS);
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg).toContain("</svg>");
    // Tables and their columns come from the model, not a canvas capture.
    expect(svg).toContain("users");
    expect(svg).toContain("orders");
    expect(svg).toContain("email");
    expect(svg).toContain("user_id");
  });

  it("shows the resolved physical type, so a uuid FK stays uuid (the project lesson)", () => {
    const svg = buildErdSvg(RENDER_MODEL, POSITIONS);
    expect(svg).toContain("varchar(255)"); // email's resolved type
    expect(svg).toContain("uuid"); // the FK column inherits the referenced PK's type, never an int
    // PK/FK markers are rendered from the field flags.
    expect(svg).toContain(">PK<");
    expect(svg).toContain(">FK<");
  });

  it("draws a relation edge with a cardinality marker", () => {
    const svg = buildErdSvg(RENDER_MODEL, POSITIONS);
    expect(svg).toContain("marker-end=\"url(#erd-arrow)\"");
    expect(svg).toContain("1 — ∞"); // one_to_many cardinality glyph
  });

  it("sizes the canvas to the diagram bounds plus a margin", () => {
    const svg = buildErdSvg(RENDER_MODEL, POSITIONS);
    const b = erdBounds(RENDER_MODEL, POSITIONS);
    expect(b.width).toBeGreaterThanOrEqual(400 + NODE_WIDTH); // two tables, 400px apart
    const m = svg.match(/width="(\d+)" height="(\d+)"/);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBe(Math.round(b.width + 80)); // MARGIN*2
  });

  it("is deterministic for the same model + positions", () => {
    expect(buildErdSvg(RENDER_MODEL, POSITIONS)).toBe(buildErdSvg(RENDER_MODEL, POSITIONS));
  });

  it("throws when there is nothing to export", () => {
    const empty: RenderModel = { ...RENDER_MODEL, tables: [], relations: [] };
    expect(() => buildErdSvg(empty, {})).toThrow(/nothing to export/i);
  });

  it("escapes XML-special characters in names", () => {
    const model: RenderModel = {
      ...RENDER_MODEL,
      tables: [{ ...RENDER_MODEL.tables[0], name: "a&b<c" }],
      relations: [],
    };
    const svg = buildErdSvg(model, { tbl_users0001: { x: 0, y: 0 } });
    expect(svg).toContain("a&amp;b&lt;c");
    expect(svg).not.toContain("a&b<c");
  });
});

describe("paginate", () => {
  it("returns a single page for a small diagram", () => {
    expect(paginate(800, 600)).toHaveLength(1);
  });

  it("tiles a large diagram into a logical grid of pages with overlap", () => {
    const pages = paginate(4000, 2000); // ~A4-landscape (1123×794) units
    expect(pages.length).toBeGreaterThan(1);
    // Row-major indexing and a consistent total on every page.
    expect(pages[0]).toMatchObject({ index: 0, row: 0, col: 0 });
    expect(pages.every((p) => p.total === pages.length)).toBe(true);
    // Pages overlap (step < page width), so a seam-straddling table appears whole on a page.
    expect(pages[1].x).toBeLessThan(pages[0].x + pages[0].w);
  });

  it("covers the full diagram width and height", () => {
    const W = 4000;
    const H = 2000;
    const pages = paginate(W, H);
    const right = Math.max(...pages.map((p) => p.x + p.w));
    const bottom = Math.max(...pages.map((p) => p.y + p.h));
    expect(right).toBeGreaterThanOrEqual(W);
    expect(bottom).toBeGreaterThanOrEqual(H);
  });
});
