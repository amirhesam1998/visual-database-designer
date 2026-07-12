import { describe, expect, it } from "vitest";
import { buildGraph, neighboursOf } from "./graph";
import { autoLayout, resolvePositions } from "./layout";
import { RENDER_MODEL } from "@/test/fixtures";
import type { RenderModel, RenderTable } from "./types";

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

describe("relations drive both the edges AND the clustering (bug §6)", () => {
  // Bug §6 (relations shown wrong / related tables thrown aside with no line): the render AND the
  // layout are both driven entirely by `model.relations` with a resolvable `toTableId`. This pins the
  // contract — a relation present in the data with valid endpoints is BOTH drawn as an edge AND keeps
  // its tables clustered together, while only a genuinely relation-less table is pushed off to the
  // isolated grid. So an FK-bearing table that shows up isolated with no edge means the relation never
  // reached the data (an import/inference gap), NOT a rendering bug.
  const tbl = (id: string, name: string): RenderTable => ({
    id,
    name,
    comment: null,
    kind: "normal",
    fields: [{
      id: `fld_${id}`, name: "id", semanticType: "uuid", physicalType: "uuid", nullable: false,
      isPrimaryKey: true, isForeignKey: false, pii: false, sensitivity: null, enumId: null, comment: null,
    }],
  });
  const model: RenderModel = {
    meta: {},
    tables: [tbl("tbl_a", "a"), tbl("tbl_b", "b"), tbl("tbl_c", "c")],
    relations: [
      { id: "rel_ab", type: "one_to_many", fromTableId: "tbl_a", toTableId: "tbl_b", foreignKeyFieldId: "fld_tbl_a", onDelete: null, onUpdate: null },
    ],
    enums: [],
    presentation: { nodes: [] },
    hasLayout: false,
  };

  it("renders an edge for the FK relation and none for the unrelated table", () => {
    const { edges } = buildGraph(model);
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "tbl_a", target: "tbl_b" });
  });

  it("clusters the related pair adjacently and pushes the relation-less table aside", () => {
    // dagre lays the a→b cluster out left-to-right (a left of b, ~one node-width apart); the isolated
    // table is shelf-packed off to the right *after* the clusters. That is the correct inverse of the
    // bug ("FK tables thrown to the side with no line"): the *unrelated* table goes aside, never the
    // connected ones.
    const pos = autoLayout(model);
    expect(pos.tbl_a.x).toBeLessThan(pos.tbl_b.x);      // related pair kept adjacent in the cluster
    expect(pos.tbl_c.x).toBeGreaterThan(pos.tbl_a.x);   // the isolated table is pushed to the right
    expect(pos.tbl_c.x).toBeGreaterThan(pos.tbl_b.x);
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
