import type { Edge, Node } from "reactflow";
import { MarkerType } from "reactflow";
import { CARDINALITY, type ChangeColor, type RenderModel, type RenderTable } from "./types";
import { NODE_WIDTH, nodeHeight, resolvePositions } from "./layout";

export interface TableNodeData {
  table: RenderTable;
  /** Milestone 2: when true the node exposes editing affordances (inline rename, connectable handles). */
  editable: boolean;
  /** Hover/selection emphasis (read-only highlight, spec §4/§5). */
  highlighted: boolean;
  dimmed: boolean;
  /** Engine validation errors on this table or its fields (Milestone 2 §6) — set in Canvas. */
  hasError: boolean;
  /** Field ids the engine flagged with an error, so the node can mark the exact row. */
  errorFieldIds: Set<string>;
  /** The engine has a design insight on this table or its fields (intelligence milestone §4). */
  hasInsight: boolean;
  /** Field ids carrying a design insight, so the node can mark the exact row in context. */
  insightFieldIds: Set<string>;
  /** Diff tint for the table and its changed fields (Milestone 3 §1) — set in Canvas from the engine diff. */
  changeColor: ChangeColor | null;
  fieldChanges: Map<string, ChangeColor>;
}

export interface RelationEdgeData {
  type: string;
  onDelete: string | null;
}

/**
 * Pure transform: render model → React Flow graph. No side effects, no data fetching — this is the
 * "render" half of `endpoint → state → render` and is what the unit tests exercise directly.
 */
export function buildGraph(model: RenderModel): {
  nodes: Node<TableNodeData>[];
  edges: Edge<RelationEdgeData>[];
} {
  const positions = resolvePositions(model);

  const nodes: Node<TableNodeData>[] = model.tables.map((table) => ({
    id: table.id,
    type: "table",
    position: positions[table.id] ?? { x: 0, y: 0 },
    // Seed dimensions so edges have geometry before the DOM is measured (also keeps the minimap
    // proportional on first paint).
    width: NODE_WIDTH,
    height: nodeHeight(table.fields.length),
    data: {
      table,
      editable: false,
      highlighted: false,
      dimmed: false,
      hasError: false,
      errorFieldIds: new Set<string>(),
      hasInsight: false,
      insightFieldIds: new Set<string>(),
      changeColor: null,
      fieldChanges: new Map<string, ChangeColor>(),
    },
  }));

  const tableIds = new Set(model.tables.map((t) => t.id));
  const edges: Edge<RelationEdgeData>[] = model.relations
    .filter((r) => r.toTableId && tableIds.has(r.fromTableId) && tableIds.has(r.toTableId))
    .map((r) => ({
      id: r.id,
      source: r.fromTableId,
      target: r.toTableId as string,
      type: "relation",
      label: CARDINALITY[r.type] ?? r.type,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      data: { type: r.type, onDelete: r.onDelete },
    }));

  return { nodes, edges };
}

/** Tables directly connected to `tableId` (used to highlight a node's relations on hover, spec §5). */
export function neighboursOf(model: RenderModel, tableId: string): {
  nodeIds: Set<string>;
  edgeIds: Set<string>;
} {
  const nodeIds = new Set<string>([tableId]);
  const edgeIds = new Set<string>();
  for (const r of model.relations) {
    if (r.fromTableId === tableId || r.toTableId === tableId) {
      edgeIds.add(r.id);
      nodeIds.add(r.fromTableId);
      if (r.toTableId) nodeIds.add(r.toTableId);
    }
  }
  return { nodeIds, edgeIds };
}
