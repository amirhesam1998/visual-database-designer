import { afterEach, describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { DiffPanel } from "./DiffPanel";
import { useCanvasStore } from "@/store/canvasStore";
import type { DiffResult } from "@/lib/types";

// The diff view renders the engine's operation list verbatim (spec §1) — colours and text come from
// `/core/diff`, the canvas computes nothing.

const DIFF: DiffResult = {
  operations: [
    { op: "add_table", tableId: "tbl_new0001", name: "carts" },
    { op: "drop_table", tableId: "tbl_old0001", name: "legacy" },
  ],
  changelog: [],
  stats: { added: 1, removed: 1, changed: 0, renamed: 0 },
  colored: [
    { color: "green", text: "add_table carts" },
    { color: "red", text: "drop_table legacy" },
  ],
  notes: [],
};

describe("DiffPanel", () => {
  afterEach(() => useCanvasStore.setState({ diff: null }));

  it("lists every engine operation with its changelog text", () => {
    useCanvasStore.setState({ diff: DIFF });
    render(<DiffPanel open onClose={() => {}} />);
    const list = screen.getByTestId("diff-list");
    expect(within(list).getByText("add_table carts")).toBeInTheDocument();
    expect(within(list).getByText("drop_table legacy")).toBeInTheDocument();
  });

  it("shows a calm empty state when there are no changes", () => {
    useCanvasStore.setState({ diff: { ...DIFF, operations: [], colored: [] } });
    render(<DiffPanel open onClose={() => {}} />);
    expect(screen.getByText(/No changes vs the approved base/i)).toBeInTheDocument();
  });

  it("renders nothing when closed", () => {
    useCanvasStore.setState({ diff: DIFF });
    const { container } = render(<DiffPanel open={false} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
