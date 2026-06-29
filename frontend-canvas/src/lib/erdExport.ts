import { downloadText, downloadUrl } from "./download";
import { HEADER_HEIGHT, NODE_WIDTH, ROW_HEIGHT, nodeHeight, type Positions } from "./layout";
import { CARDINALITY, type RenderModel, type RenderRelation, type RenderTable } from "./types";

// Vector ERD export (close-debts milestone §1). The previous export screenshotted the React Flow DOM
// with html-to-image and crammed the PNG onto one PDF page — for a large map (100+ tables) that means
// cropping, blur or a single unusable giant image. We instead render a **clean SVG straight from the
// schema data** (the render model) + the canvas node positions: vector, so it stays crisp at any size,
// and the layout is exactly what the user sees. PDF supports real **pagination** (a grid of pages with
// page numbers + slight overlap) so a big diagram prints legibly. Still display-only — like the
// presentation layer it depends on layout and never touches the schema, diff or migration.

export type ErdFormat = "svg" | "png" | "pdf";

export interface ErdTheme {
  background: string;
  tableBg: string;
  headerBg: string;
  headerText: string;
  border: string;
  text: string;
  muted: string;
  pk: string;
  fk: string;
  pii: string;
  edge: string;
}

export interface ErdOptions {
  name?: string;
  /** PDF only: lay the diagram across multiple real-size pages instead of one scaled page (spec §1.2). */
  paged?: boolean;
  theme?: Partial<ErdTheme>;
}

// Sensible light-theme defaults; `resolveTheme()` overrides these from the live CSS variables so the
// export matches the current (light/dark) canvas. Kept explicit so `buildErdSvg` stays pure & testable.
const DEFAULT_THEME: ErdTheme = {
  background: "#ffffff",
  tableBg: "#ffffff",
  headerBg: "#1e293b",
  headerText: "#f8fafc",
  border: "#cbd5e1",
  text: "#0f172a",
  muted: "#64748b",
  pk: "#d97706",
  fk: "#2563eb",
  pii: "#dc2626",
  edge: "#94a3b8",
};

const MARGIN = 40; // breathing room around the diagram bounds
const RASTER_SCALE = 2; // PNG/PDF rasterisation factor (crisp at 2× device pixels)
const PAGE = { w: 1123, h: 794 }; // A4 landscape at 96dpi (px) — the PDF page grid unit
const PAGE_OVERLAP = 48; // px of shared edge between adjacent pages so nothing is lost at a seam

interface Box {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  table: RenderTable;
}

function boxesOf(model: RenderModel, positions: Positions): Box[] {
  return model.tables.map((t) => {
    const p = positions[t.id] ?? { x: 0, y: 0 };
    return { id: t.id, x: p.x, y: p.y, w: NODE_WIDTH, h: nodeHeight(t.fields.length), table: t };
  });
}

export interface ErdBounds {
  minX: number;
  minY: number;
  width: number;
  height: number;
}

/** The bounding box of every table node (before the export margin is added). */
export function erdBounds(model: RenderModel, positions: Positions): ErdBounds {
  const boxes = boxesOf(model, positions);
  if (boxes.length === 0) return { minX: 0, minY: 0, width: 0, height: 0 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const b of boxes) {
    minX = Math.min(minX, b.x);
    minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.w);
    maxY = Math.max(maxY, b.y + b.h);
  }
  return { minX, minY, width: maxX - minX, height: maxY - minY };
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fieldRow(f: RenderTable["fields"][number], y: number, theme: ErdTheme): string {
  const key = f.isPrimaryKey ? "PK" : f.isForeignKey ? "FK" : "";
  const keyColor = f.isPrimaryKey ? theme.pk : theme.fk;
  const nameColor = f.pii ? theme.pii : theme.text;
  const lock = f.pii ? `<tspan fill="${theme.pii}"> 🔒</tspan>` : "";
  const keyCell = key
    ? `<text x="10" y="${y + 17}" font-size="9" font-weight="700" fill="${keyColor}">${key}</text>`
    : "";
  return (
    keyCell +
    `<text x="34" y="${y + 17}" font-size="11" fill="${nameColor}">${esc(f.name)}${lock}</text>` +
    `<text x="${NODE_WIDTH - 10}" y="${y + 17}" font-size="10" text-anchor="end" fill="${theme.muted}">` +
    `${esc(f.physicalType || f.semanticType)}</text>`
  );
}

function tableSvg(b: Box, dx: number, dy: number, theme: ErdTheme): string {
  const x = b.x + dx;
  const y = b.y + dy;
  const rows = b.table.fields
    .map((f, i) => fieldRow(f, HEADER_HEIGHT + i * ROW_HEIGHT, theme))
    .join("");
  return (
    `<g transform="translate(${x},${y})">` +
    `<rect width="${b.w}" height="${b.h}" rx="8" fill="${theme.tableBg}" stroke="${theme.border}" stroke-width="1"/>` +
    `<path d="M0 8 a8 8 0 0 1 8 -8 h${b.w - 16} a8 8 0 0 1 8 8 v${HEADER_HEIGHT - 8} h-${b.w} z" fill="${theme.headerBg}"/>` +
    `<text x="12" y="26" font-size="13" font-weight="700" fill="${theme.headerText}">${esc(b.table.name)}</text>` +
    `<line x1="0" y1="${HEADER_HEIGHT}" x2="${b.w}" y2="${HEADER_HEIGHT}" stroke="${theme.border}"/>` +
    rows +
    `</g>`
  );
}

function edgeSvg(rel: RenderRelation, byId: Map<string, Box>, dx: number, dy: number, theme: ErdTheme): string {
  if (!rel.toTableId) return "";
  const from = byId.get(rel.fromTableId);
  const to = byId.get(rel.toTableId);
  if (!from || !to || from === to) return ""; // self-relations aren't drawn as an edge
  const fcx = from.x + from.w / 2;
  const tcx = to.x + to.w / 2;
  // Exit/enter on the facing vertical edges so the line never crosses through a node body.
  const rightward = tcx >= fcx;
  const x1 = (rightward ? from.x + from.w : from.x) + dx;
  const y1 = from.y + from.h / 2 + dy;
  const x2 = (rightward ? to.x : to.x + to.w) + dx;
  const y2 = to.y + to.h / 2 + dy;
  const c = Math.max(40, Math.abs(x2 - x1) / 2); // bezier control offset
  const cx1 = rightward ? x1 + c : x1 - c;
  const cx2 = rightward ? x2 - c : x2 + c;
  const label = CARDINALITY[rel.type] ?? "";
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  return (
    `<path d="M${x1} ${y1} C${cx1} ${y1} ${cx2} ${y2} ${x2} ${y2}" fill="none" ` +
    `stroke="${theme.edge}" stroke-width="1.5" marker-end="url(#erd-arrow)"/>` +
    (label
      ? `<text x="${mx}" y="${my - 4}" font-size="9" text-anchor="middle" fill="${theme.muted}">${esc(label)}</text>`
      : "")
  );
}

/**
 * Build a clean, self-contained SVG of the schema from the render model + node positions. Pure and
 * deterministic (no DOM, no screenshot) — the one true vector artifact every other format derives from.
 */
export function buildErdSvg(model: RenderModel, positions: Positions, opts: ErdOptions = {}): string {
  const theme = { ...DEFAULT_THEME, ...(opts.theme ?? {}) };
  const boxes = boxesOf(model, positions);
  if (boxes.length === 0) {
    throw new Error("Nothing to export — add some tables to the canvas first.");
  }
  const b = erdBounds(model, positions);
  const width = Math.round(b.width + MARGIN * 2);
  const height = Math.round(b.height + MARGIN * 2);
  const dx = MARGIN - b.minX;
  const dy = MARGIN - b.minY;
  const byId = new Map(boxes.map((box) => [box.id, box]));
  const edges = model.relations.map((r) => edgeSvg(r, byId, dx, dy, theme)).join("");
  const tables = boxes.map((box) => tableSvg(box, dx, dy, theme)).join("");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" ` +
    `font-family="ui-sans-serif, system-ui, sans-serif">` +
    `<defs><marker id="erd-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
    `<path d="M0 0 L10 5 L0 10 z" fill="${theme.edge}"/></marker></defs>` +
    `<rect width="${width}" height="${height}" fill="${theme.background}"/>` +
    `<g class="erd-edges">${edges}</g>` +
    `<g class="erd-tables">${tables}</g>` +
    `</svg>`
  );
}

export interface PageRect {
  index: number;
  total: number;
  row: number;
  col: number;
  cols: number;
  rows: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * Tile a diagram of the given size into a grid of fixed-size pages (row-major), with a small overlap so
 * a table straddling a seam is fully visible on at least one page (spec §1.2). A diagram that fits one
 * page yields exactly one page.
 */
export function paginate(width: number, height: number, page = PAGE, overlap = PAGE_OVERLAP): PageRect[] {
  const stepX = Math.max(1, page.w - overlap);
  const stepY = Math.max(1, page.h - overlap);
  const cols = width <= page.w ? 1 : Math.ceil((width - overlap) / stepX);
  const rows = height <= page.h ? 1 : Math.ceil((height - overlap) / stepY);
  const pages: PageRect[] = [];
  let index = 0;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      pages.push({
        index: index++,
        total: cols * rows,
        row,
        col,
        cols,
        rows,
        x: col * stepX,
        y: row * stepY,
        w: page.w,
        h: page.h,
      });
    }
  }
  return pages;
}

/** Resolve the live CSS theme variables into concrete colours so the export matches the canvas. */
function resolveTheme(): Partial<ErdTheme> {
  if (typeof window === "undefined" || typeof document === "undefined") return {};
  const css = getComputedStyle(document.documentElement);
  const v = (name: string): string | undefined => {
    const raw = css.getPropertyValue(name).trim();
    return raw ? `hsl(${raw})` : undefined;
  };
  const out: Partial<ErdTheme> = {};
  const bg = v("--background");
  const card = v("--card");
  const border = v("--border");
  const fg = v("--foreground");
  const muted = v("--muted-foreground");
  const primary = v("--primary");
  if (bg) out.background = bg;
  if (card) out.tableBg = card;
  if (border) out.border = border;
  if (fg) {
    out.text = fg;
    out.headerBg = fg; // header bar uses the foreground colour; its text flips to the background
  }
  if (bg) out.headerText = bg;
  if (muted) out.muted = muted;
  if (primary) out.fk = primary;
  return out;
}

/** Rasterise an SVG string to a loaded <img> at the given pixel size (browser-only). */
function svgToImage(svg: string, pxWidth: number, pxHeight: number): Promise<HTMLImageElement> {
  // The SVG carries logical width/height; swap in the raster size so the browser rasterises the vector
  // at high resolution (drawing a logical-size image onto a larger canvas would blur it).
  const sized = svg
    .replace(/width="\d+"/, `width="${pxWidth}"`)
    .replace(/height="\d+"/, `height="${pxHeight}"`);
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(sized);
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("failed to rasterise the ERD SVG"));
    img.src = url;
  });
}

/** Draw a (region of a) rasterised image onto a fresh canvas filled with the background. */
function paintCanvas(
  img: HTMLImageElement,
  destW: number,
  destH: number,
  background: string,
  source?: { sx: number; sy: number; sw: number; sh: number },
): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(destW));
  canvas.height = Math.max(1, Math.round(destH));
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (source) {
      ctx.drawImage(img, source.sx, source.sy, source.sw, source.sh, 0, 0, canvas.width, canvas.height);
    } else {
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    }
  }
  return canvas;
}

/**
 * Export the current diagram. SVG is the clean vector artifact; PNG rasterises it at 2×; PDF either
 * fits the whole diagram on one scaled page or (when `paged`) tiles it across real-size A4 pages with
 * page numbers. Heavy libs (jsPDF) are imported on demand so they stay out of the initial bundle.
 */
export async function exportErd(
  format: ErdFormat,
  model: RenderModel,
  positions: Positions,
  opts: ErdOptions = {},
): Promise<void> {
  const theme = { ...DEFAULT_THEME, ...resolveTheme(), ...(opts.theme ?? {}) };
  const svg = buildErdSvg(model, positions, { ...opts, theme });
  const name = opts.name?.trim() || "erd";

  if (format === "svg") {
    downloadText(`${name}.svg`, svg, "image/svg+xml");
    return;
  }

  const b = erdBounds(model, positions);
  const width = Math.round(b.width + MARGIN * 2);
  const height = Math.round(b.height + MARGIN * 2);
  const img = await svgToImage(svg, width * RASTER_SCALE, height * RASTER_SCALE);

  if (format === "png") {
    const canvas = paintCanvas(img, width * RASTER_SCALE, height * RASTER_SCALE, theme.background);
    downloadUrl(`${name}.png`, canvas.toDataURL("image/png"));
    return;
  }

  const { jsPDF } = await import("jspdf");

  if (!opts.paged) {
    // One page scaled to the whole diagram (good for small/medium maps).
    const canvas = paintCanvas(img, width * RASTER_SCALE, height * RASTER_SCALE, theme.background);
    const pdf = new jsPDF({ orientation: width >= height ? "landscape" : "portrait", unit: "px", format: [width, height] });
    pdf.addImage(canvas.toDataURL("image/png"), "PNG", 0, 0, width, height);
    pdf.save(`${name}.pdf`);
    return;
  }

  // Multi-page: tile the diagram across A4 landscape pages at real size (spec §1.2 — no crushed page).
  const pages = paginate(width, height);
  const pdf = new jsPDF({ orientation: "landscape", unit: "px", format: [PAGE.w, PAGE.h] });
  pages.forEach((pg, i) => {
    if (i > 0) pdf.addPage([PAGE.w, PAGE.h], "landscape");
    const slice = paintCanvas(img, pg.w * RASTER_SCALE, pg.h * RASTER_SCALE, theme.background, {
      sx: pg.x * RASTER_SCALE,
      sy: pg.y * RASTER_SCALE,
      sw: pg.w * RASTER_SCALE,
      sh: pg.h * RASTER_SCALE,
    });
    pdf.addImage(slice.toDataURL("image/png"), "PNG", 0, 0, pg.w, pg.h);
    pdf.setFontSize(8);
    pdf.setTextColor(120);
    pdf.text(`${name} — page ${i + 1}/${pg.total} (row ${pg.row + 1}, col ${pg.col + 1})`, 12, pg.h - 10);
  });
  pdf.save(`${name}.pdf`);
}
