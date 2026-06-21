import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ApproveDialog } from "./ApproveDialog";
import { useCanvasStore } from "@/store/canvasStore";
import type { SchemaDoc } from "@/lib/schema";

// The approve dialog SHOWS the engine's risk and reflects the gate's critical guard (spec §2/§3):
// a critical migration cannot be approved until the user explicitly acknowledges it — the button
// stays disabled, mirroring the engine's `acknowledgeCritical`.

const CRITICAL_RISK = {
  driver: "postgres",
  operations: [
    {
      op: "drop_table",
      target: "tbl_orders001",
      level: "critical",
      dimensions: ["data_loss"],
      reversible: false,
      requires_backup: true,
      explanation: { en: "Dropping a table destroys all of its data." },
      safe_plan: [],
    },
  ],
  summary: { critical: 1 },
  max_level: "critical",
  exit_code: 2,
  checklist: [],
};

const doc: SchemaDoc = { formatVersion: "1.0.0", logical: { tables: [], relations: [] } };

describe("ApproveDialog (critical migration needs acknowledgement)", () => {
  let runApprove: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/core/risk")) return new Response(JSON.stringify(CRITICAL_RISK), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as never;
    runApprove = vi.fn(async () => ({ status: "approved", schemaVersion: "v1" }));
    useCanvasStore.setState({
      baseline: doc,
      doc,
      diff: { operations: [{ op: "drop_table", tableId: "tbl_orders001" }], colored: [], changelog: [], notes: [], stats: { added: 0, removed: 1, changed: 0, renamed: 0 } },
      runApprove: runApprove as never,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("disables Approve until the critical risk is acknowledged, then approves", async () => {
    render(<ApproveDialog open onClose={() => {}} />);

    // engine risk is shown (display, not decision)
    expect(await screen.findByText(/critical risk/i)).toBeInTheDocument();
    expect(screen.getByText(/destroys all of its data/i)).toBeInTheDocument();

    const approveBtn = screen.getByRole("button", { name: /^approve$/i });
    expect(approveBtn).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/acknowledge critical migration risk/i));
    expect(approveBtn).toBeEnabled();

    fireEvent.click(approveBtn);
    expect(runApprove).toHaveBeenCalledWith(true);
  });
});
