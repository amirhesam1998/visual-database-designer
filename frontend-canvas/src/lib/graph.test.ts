import { describe, expect, it } from "vitest";
import { buildGraph, neighboursOf } from "./graph";
import { resolvePositions } from "./layout";
import { RENDER_MODEL } from "@/test/fixtures";
import type { RenderModel } from "./types";

describe("buildGraph", () => {
  it("turns every table into a node and every (resolvable) relation into a directed edge", () => {
    const { nodes, edges } = buildGraph(RENDER_MODEL);
    expect(nodes.map((n) => n.id).sort()).toEqual(["tbl_orders001", "tbl_users0001"]);
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "tbl_orders001", target: "tbl_users0001" });
    expect(edges[0].label).toBe("1 — ∞"); // one_to_many cardinality marker
    expect(edges[0].markerEnd).toBeDefined(); // direction arrow
  });

  it("carries the resolved FK physical type through to node data (uuid, not integer)", () => {
    const { nodes } = buildGraph(RENDER_MODEL);
    const orders = nodes.find((n) => n.id === "tbl_orders001")!;
    const fk = orders.data.table.fields.find((f) => f.name === "user_id")!;
    expect(fk.isForeignKey).toBe(true);
    expect(fk.physicalType).toBe("uuid");
  });

  it("starts nodes non-editable and un-highlighted (Milestone-1 read-only defaults)", () => {
    const { nodes } = buildGraph(RENDER_MODEL);
    expect(nodes.every((n) => n.data.editable === false)).toBe(true);
    expect(nodes.every((n) => n.data.highlighted === false)).toBe(true);
  });

  it("drops relations whose endpoint table is missing", () => {
    const broken: RenderModel = {
      ...RENDER_MODEL,
      relations: [
        ...RENDER_MODEL.relations,
        { id: "rel_x", type: "one_to_many", fromTableId: "tbl_orders001", toTableId: "tbl_ghost", foreignKeyFieldId: null, onDelete: null, onUpdate: null },
      ],
    };
    expect(buildGraph(broken).edges).toHaveLength(1);
  });
});

describe("layout", () => {
  it("auto-lays-out (non-overlapping, deterministic) when there are no saved positions", () => {
    const a = resolvePositions(RENDER_MODEL);
    const b = resolvePositions(RENDER_MODEL);
    expect(a).toEqual(b); // deterministic
    const [p1, p2] = Object.values(a);
    expect(p1.x !== p2.x || p1.y !== p2.y).toBe(true); // not stacked on the same spot
  });

  it("uses saved presentation positions verbatim when present", () => {
    const withLayout: RenderModel = {
      ...RENDER_MODEL,
      hasLayout: true,
      presentation: {
        nodes: [
          { tableId: "tbl_users0001", x: 11, y: 22 },
          { tableId: "tbl_orders001", x: 333, y: 444 },
        ],
      },
    };
    expect(resolvePositions(withLayout)).toEqual({
      tbl_users0001: { x: 11, y: 22 },
      tbl_orders001: { x: 333, y: 444 },
    });
  });
});

describe("neighboursOf", () => {
  it("collects a table plus its connected tables and edges (for hover highlight)", () => {
    const { nodeIds, edgeIds } = neighboursOf(RENDER_MODEL, "tbl_users0001");
    expect(nodeIds).toContain("tbl_users0001");
    expect(nodeIds).toContain("tbl_orders001");
    expect(edgeIds).toContain("rel_order_usr");
  });
});
