import dagre from "dagre";
import type { RenderModel } from "./types";

export const NODE_WIDTH = 252;
export const ROW_HEIGHT = 26;
export const HEADER_HEIGHT = 42;

/** Estimated rendered height of a table node, so the layout engine spaces them without overlap. */
export function nodeHeight(fieldCount: number): number {
  return HEADER_HEIGHT + Math.max(fieldCount, 1) * ROW_HEIGHT + 10;
}

export type Positions = Record<string, { x: number; y: number }>;

const COMPONENT_GAP = 80; // gap between independent clusters / the isolated-table grid
const ISO_GAP = 24; // gap between isolated tables packed into a grid

type Block = { width: number; height: number; positions: Positions };

/**
 * Deterministic auto-layout for schemas without saved `presentation` positions (spec §1/§3 — readable
 * at scale: clustering by relationships, not a random or one-giant-rank scatter). Positions are
 * *display only* and never flow back into the schema.
 *
 * For large maps (e.g. 112 imported tables) a single dagre pass produces an unreadable sprawl, so we:
 *   1. split the schema into connected components (clusters of related tables);
 *   2. lay out each multi-table cluster with dagre (layered, related tables adjacent);
 *   3. grid-pack all fully-isolated tables (no relations) into a compact block;
 *   4. shelf-pack the resulting blocks into rows so clusters sit side by side, not on top of each other.
 */
export function autoLayout(model: RenderModel): Positions {
  const ids = model.tables.map((t) => t.id);
  const heightOf = new Map(model.tables.map((t) => [t.id, nodeHeight(t.fields.length)]));

  // Undirected adjacency (self-loops ignored) → connected components.
  const adj = new Map<string, Set<string>>(ids.map((id) => [id, new Set<string>()]));
  for (const r of model.relations) {
    if (r.toTableId && r.fromTableId !== r.toTableId && adj.has(r.fromTableId) && adj.has(r.toTableId)) {
      adj.get(r.fromTableId)!.add(r.toTableId);
      adj.get(r.toTableId)!.add(r.fromTableId);
    }
  }
  const seen = new Set<string>();
  const components: string[][] = [];
  for (const id of ids) {
    if (seen.has(id)) continue;
    const stack = [id];
    const group: string[] = [];
    seen.add(id);
    while (stack.length) {
      const cur = stack.pop()!;
      group.push(cur);
      for (const nb of [...adj.get(cur)!].sort()) {
        if (!seen.has(nb)) { seen.add(nb); stack.push(nb); }
      }
    }
    components.push(group.sort()); // sort for determinism
  }

  // Directed edges (self-loops excluded) used to drive dagre within each cluster.
  const edges: Array<[string, string]> = [];
  for (const r of model.relations) {
    if (r.toTableId && r.fromTableId !== r.toTableId && adj.has(r.fromTableId) && adj.has(r.toTableId)) {
      edges.push([r.fromTableId, r.toTableId]);
    }
  }

  // Build one block per connected cluster (size > 1), and gather isolated tables for a grid block.
  const blocks: Block[] = [];
  const isolated: string[] = [];
  for (const group of components) {
    if (group.length === 1) { isolated.push(group[0]); continue; }
    blocks.push(layoutCluster(group, edges, heightOf));
  }
  // Largest clusters first → tighter, more stable shelf packing.
  blocks.sort((a, b) => b.height - a.height || b.width - a.width);
  if (isolated.length) blocks.push(gridPack(isolated.sort(), heightOf));

  return shelfPack(blocks);
}

/** Lay out one connected cluster with dagre; return its block (top-left-relative positions + size). */
function layoutCluster(group: string[], edges: Array<[string, string]>, heightOf: Map<string, number>): Block {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 90, marginx: 12, marginy: 12 });
  g.setDefaultEdgeLabel(() => ({}));
  const inGroup = new Set(group);
  for (const id of group) g.setNode(id, { width: NODE_WIDTH, height: heightOf.get(id) ?? ROW_HEIGHT });
  for (const [from, to] of edges) {
    if (inGroup.has(from) && inGroup.has(to)) g.setEdge(from, to);
  }
  return finishCluster(g, group, heightOf);
}

/** Run dagre and translate centre coords to top-left, normalised so the block starts at (0,0). */
function finishCluster(g: dagre.graphlib.Graph, group: string[], heightOf: Map<string, number>): Block {
  dagre.layout(g);
  const positions: Positions = {};
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const id of group) {
    const n = g.node(id);
    const h = heightOf.get(id) ?? ROW_HEIGHT;
    const x = n ? n.x - NODE_WIDTH / 2 : 0;
    const y = n ? n.y - h / 2 : 0;
    positions[id] = { x, y };
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + NODE_WIDTH); maxY = Math.max(maxY, y + h);
  }
  for (const id of group) { positions[id] = { x: positions[id].x - minX, y: positions[id].y - minY }; }
  return { width: maxX - minX, height: maxY - minY, positions };
}

/** Pack fully-isolated tables (no relations) into a compact grid block. */
function gridPack(iso: string[], heightOf: Map<string, number>): Block {
  const cols = Math.max(1, Math.ceil(Math.sqrt(iso.length)));
  const positions: Positions = {};
  const rowH: number[] = [];
  let width = 0;
  for (let i = 0; i < iso.length; i++) {
    const row = Math.floor(i / cols);
    rowH[row] = Math.max(rowH[row] ?? 0, heightOf.get(iso[i]) ?? ROW_HEIGHT);
  }
  const rowY: number[] = [];
  let y = 0;
  for (let r = 0; r < rowH.length; r++) { rowY[r] = y; y += rowH[r] + ISO_GAP; }
  for (let i = 0; i < iso.length; i++) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const x = col * (NODE_WIDTH + ISO_GAP);
    positions[iso[i]] = { x, y: rowY[row] };
    width = Math.max(width, x + NODE_WIDTH);
  }
  return { width, height: y > 0 ? y - ISO_GAP : 0, positions };
}

/** Shelf-pack blocks into rows (left→right, wrapping) so clusters sit beside each other, not stacked. */
function shelfPack(blocks: Block[]): Positions {
  const totalArea = blocks.reduce((s, b) => s + b.width * b.height, 0);
  const maxRowWidth = Math.max(2400, Math.sqrt(totalArea) * 1.5);
  const out: Positions = {};
  let cursorX = 0;
  let shelfY = 0;
  let shelfH = 0;
  for (const block of blocks) {
    if (cursorX > 0 && cursorX + block.width > maxRowWidth) {
      shelfY += shelfH + COMPONENT_GAP; // wrap to a new shelf
      cursorX = 0;
      shelfH = 0;
    }
    for (const [id, p] of Object.entries(block.positions)) {
      out[id] = { x: cursorX + p.x, y: shelfY + p.y };
    }
    cursorX += block.width + COMPONENT_GAP;
    shelfH = Math.max(shelfH, block.height);
  }
  return out;
}

/** Use saved presentation positions when present (spec §3), else fall back to auto-layout. */
export function resolvePositions(model: RenderModel): Positions {
  if (model.hasLayout && model.presentation.nodes.length > 0) {
    const positions: Positions = {};
    for (const n of model.presentation.nodes) positions[n.tableId] = { x: n.x, y: n.y };
    // Any table missing a saved position still needs one — fill gaps from auto-layout.
    const missing = model.tables.some((t) => !(t.id in positions));
    if (!missing) return positions;
    const auto = autoLayout(model);
    for (const t of model.tables) if (!(t.id in positions)) positions[t.id] = auto[t.id];
    return positions;
  }
  return autoLayout(model);
}
