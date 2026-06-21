import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "./App";
import { useCanvasStore } from "@/store/canvasStore";
import { RENDER_MODEL } from "@/test/fixtures";

// End-to-end of the front-end data path: App → fetchRenderModel (mocked) → store → React Flow.
// This is the spec §7 acceptance proof at the UI level: given a schema, the tables and edges render.
describe("App", () => {
  beforeEach(() => {
    useCanvasStore.setState({ model: null, status: "idle", error: null, selectedTableId: null });
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(RENDER_MODEL), { status: 200 })) as never;
  });
  afterEach(() => vi.restoreAllMocks());

  it("fetches the render model and draws the schema (tables + edge)", async () => {
    render(<App />);
    expect(await screen.findByTestId("table-node-users")).toBeInTheDocument();
    expect(screen.getByTestId("table-node-orders")).toBeInTheDocument();
    expect(screen.getByText("1 — ∞")).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/design/render",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows an error state if the endpoint fails, with a retry", async () => {
    globalThis.fetch = vi.fn(async () => new Response("boom", { status: 500 })) as never;
    render(<App />);
    expect(await screen.findByText(/Couldn't load the schema/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("shows the empty state when the schema has no tables", async () => {
    const empty = { ...RENDER_MODEL, tables: [], relations: [] };
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(empty), { status: 200 })) as never;
    render(<App />);
    expect(await screen.findByText(/No schema yet/i)).toBeInTheDocument();
  });
});
