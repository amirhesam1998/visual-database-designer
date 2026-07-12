import { useCallback, useEffect, useMemo, useState, type MouseEvent } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from "reactflow";
import "reactflow/dist/style.css";
import { TableNode } from "./TableNode";
import { RelationEdge } from "./RelationEdge";
import { RelationDialog, type PendingConnection } from "./RelationDialog";
import { buildGraph, type RelationEdgeData, type TableNodeData } from "@/lib/graph";
import { NODE_WIDTH, nodeHeight } from "@/lib/layout";
import {
  diffColors,
  driftColors,
  findingsByEntity,
  focusNeighbours,
  insightsByEntity,
  isErrorSeverity,
  matchingTableIds,
  useCanvasStore,
} from "@/store/canvasStore";
import type { ChangeColor, RenderModel } from "@/lib/types";

// Stroke colour for a changed relation edge (mirrors lib/diffStyle for SVG paths).
const EDGE_STROKE: Record<ChangeColor, string> = {
  green: "#10b981",
  red: "#f43f5e",
  yellow: "#f59e0b",
  blue: "#0ea5e9",
};

const nodeTypes = { table: TableNode };
const edgeTypes = { relation: RelationEdge };

export function Canvas({ model }: { model: RenderModel }) {
  const base = useMemo(() => buildGraph(model), [model]);
  const [nodes, setNodes, onNodesChange] = useNodesState<TableNodeData>(base.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RelationEdgeData>(base.edges);
  const { getNodes, fitBounds } = useReactFlow();

  // Re-seed the graph whenever a new (engine-resolved) schema arrives (endpoint → state → render).
  useEffect(() => {
    setNodes(base.nodes);
    setEdges(base.edges);
  }, [base, setNodes, setEdges]);

  const select = useCanvasStore((s) => s.select);
  const hover = useCanvasStore((s) => s.hover);
  const selectedTableId = useCanvasStore((s) => s.selectedTableId);
  const hoveredTableId = useCanvasStore((s) => s.hoveredTableId);
  const search = useCanvasStore((s) => s.search);
  const focusNonce = useCanvasStore((s) => s.focusNonce);
  const editable = useCanvasStore((s) => s.editable);
  const validation = useCanvasStore((s) => s.validation);
  const insights = useCanvasStore((s) => s.insights);
  const diff = useCanvasStore((s) => s.diff);
  const drift = useCanvasStore((s) => s.drift);
  const commitPositions = useCanvasStore((s) => s.commitPositions);
  const removeTable = useCanvasStore((s) => s.removeTable);
  const removeRelation = useCanvasStore((s) => s.removeRelation);
  const addTable = useCanvasStore((s) => s.addTable);

  const [pending, setPending] = useState<PendingConnection | null>(null);

  // Derive read-only emphasis (unchanged from M1) and, in edit mode, where the engine reported
  // errors so the exact field/table can be marked (spec §6). Nothing here decides validity.
  const focusId = hoveredTableId ?? selectedTableId;
  const neighbours = useMemo(() => focusNeighbours(model, focusId), [model, focusId]);
  const matching = useMemo(() => matchingTableIds(model, search), [model, search]);

  // Centre + frame a set of tables by computing their bounding box from the *known* node geometry
  // (positions from the layout, sizes seeded in buildGraph). This is deliberately independent of
  // React Flow's measured internals: with `onlyRenderVisibleElements` a target that is off-screen has
  // never been measured, so `fitView({nodes})` could not frame it — that was the large-project
  // search-jump bug (§5). `fitBounds` over geometry we already hold always works. (`base` carries the
  // freshly-resolved positions, so a just-created table is included before the local nodes re-seed.)
  const jumpTo = useCallback(
    (ids: Iterable<string>) => {
      const want = new Set(ids);
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of base.nodes) {
        if (!want.has(n.id)) continue;
        const w = n.width ?? NODE_WIDTH;
        const h = n.height ?? nodeHeight(n.data.table.fields.length);
        minX = Math.min(minX, n.position.x);
        minY = Math.min(minY, n.position.y);
        maxX = Math.max(maxX, n.position.x + w);
        maxY = Math.max(maxY, n.position.y + h);
      }
      if (minX === Infinity) return; // none of the ids are on the canvas
      fitBounds(
        { x: minX, y: minY, width: Math.max(maxX - minX, 1), height: Math.max(maxY - minY, 1) },
        { duration: 400, padding: 0.4 },
      );
    },
    [base, fitBounds],
  );

  // Search jumps the viewport to the matching tables and frames them (spec §1.1 — "search that jumps
  // to the table and highlights it"). Highlighting is handled by the dimming of non-matches below.
  useEffect(() => {
    if (!search?.trim() || !matching || matching.size === 0) return;
    jumpTo(matching);
  }, [search, matching, jumpTo]);

  // Bring a programmatically-focused table into view (bug §5 — after creating/duplicating a table, or
  // any explicit "go to this table"). Driven by a nonce so repeated focus of the same id still fires.
  useEffect(() => {
    if (focusNonce > 0 && selectedTableId) jumpTo([selectedTableId]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNonce]);
  const findings = useMemo(() => findingsByEntity(validation), [validation]);
  const insightMap = useMemo(() => insightsByEntity(insights), [insights]);
  // Tint = the engine's diff (vs the base) merged with a live-database drift report (vs the DB), both
  // in the same colour language. Drift wins where they overlap — it's the user's current comparison.
  const colors = useMemo(() => {
    const base = diffColors(diff);
    const d = driftColors(drift, model);
    return {
      tables: new Map([...base.tables, ...d.tables]),
      fields: new Map([...base.fields, ...d.fields]),
      relations: new Map([...base.relations, ...d.relations]),
    };
  }, [diff, drift, model]);

  const displayNodes = useMemo<Node<TableNodeData>[]>(
    () =>
      nodes.map((n) => {
        const inFocus = !neighbours || neighbours.nodeIds.has(n.id);
        const inSearch = !matching || matching.has(n.id);
        const active = inFocus && inSearch;
        const highlighted = !!neighbours && neighbours.nodeIds.has(n.id);

        const errorFieldIds = new Set<string>();
        const insightFieldIds = new Set<string>();
        for (const f of n.data.table.fields) {
          if (findings.get(f.id)?.some((x) => isErrorSeverity(x.severity))) errorFieldIds.add(f.id);
          if (insightMap.has(f.id)) insightFieldIds.add(f.id);
        }
        const hasError =
          errorFieldIds.size > 0 ||
          !!findings.get(n.id)?.some((x) => isErrorSeverity(x.severity));
        const hasInsight = insightFieldIds.size > 0 || insightMap.has(n.id);

        return {
          ...n,
          data: {
            ...n.data,
            editable,
            highlighted,
            dimmed: !active,
            hasError,
            errorFieldIds,
            hasInsight,
            insightFieldIds,
            changeColor: colors.tables.get(n.id) ?? null,
            fieldChanges: colors.fields,
          },
          className: active ? undefined : "is-dimmed",
        };
      }),
    [nodes, neighbours, matching, editable, findings, insightMap, colors],
  );

  const displayEdges = useMemo<Edge<RelationEdgeData>[]>(
    () =>
      edges.map((edge) => {
        const highlighted = !!neighbours && neighbours.edgeIds.has(edge.id);
        const dimmed =
          (!!neighbours && !neighbours.edgeIds.has(edge.id)) ||
          (!!matching && !(matching.has(edge.source) && matching.has(edge.target)));
        const change = colors.relations.get(edge.id);
        return {
          ...edge,
          animated: highlighted,
          className: highlighted ? "is-highlighted" : dimmed ? "is-dimmed" : undefined,
          style: change ? { stroke: EDGE_STROKE[change], strokeWidth: 2.5 } : undefined,
        };
      }),
    [edges, neighbours, matching, colors],
  );

  const onNodeClick: NodeMouseHandler = (_, node) => select(node.id);
  const onNodeMouseEnter: NodeMouseHandler = (_, node) => hover(node.id);

  // Dragging from a source handle onto another table opens the relation dialog (the FK field is
  // chosen there). We never add the edge directly — it appears after the engine re-renders.
  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target && c.source !== c.target) {
      setPending({ source: c.source, target: c.target });
    }
  }, []);

  // A table move persists to the presentation layer only (spec §4) — never a schema change.
  const onNodeDragStop = useCallback(() => {
    const positions: Record<string, { x: number; y: number }> = {};
    for (const n of getNodes()) positions[n.id] = { x: n.position.x, y: n.position.y };
    commitPositions(positions);
  }, [getNodes, commitPositions]);

  const onNodesDelete = useCallback(
    (deleted: Node[]) => deleted.forEach((n) => void removeTable(n.id)),
    [removeTable],
  );
  const onEdgesDelete = useCallback(
    (deleted: Edge[]) => deleted.forEach((e) => void removeRelation(e.id)),
    [removeRelation],
  );

  const onPaneDoubleClick = useCallback(
    (e: MouseEvent<HTMLDivElement>) => {
      if (editable && (e.target as HTMLElement).classList.contains("react-flow__pane")) {
        void addTable("new_table");
      }
    },
    [editable, addTable],
  );

  return (
    <div className="h-full w-full" onDoubleClick={onPaneDoubleClick}>
      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onNodeMouseEnter={onNodeMouseEnter}
        onNodeMouseLeave={() => hover(null)}
        onPaneClick={() => select(null)}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodesDelete={onNodesDelete}
        onEdgesDelete={onEdgesDelete}
        nodesConnectable={editable}
        nodesDraggable
        elementsSelectable
        deleteKeyCode={editable ? ["Delete"] : null}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        minZoom={0.05}
        maxZoom={2}
        // Virtualise only large maps (render just what's on screen) — keeps 100+ tables smooth without
        // changing behaviour for small schemas (and avoids breaking headless render in jsdom tests).
        onlyRenderVisibleElements={model.tables.length > 50}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="hsl(var(--canvas-dots))" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!bg-card"
          nodeColor="hsl(var(--muted-foreground))"
          maskColor="hsl(var(--background) / 0.6)"
        />
      </ReactFlow>
      <RelationDialog pending={pending} onClose={() => setPending(null)} />
    </div>
  );
}
