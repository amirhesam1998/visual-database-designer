import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RelationDialog } from "./RelationDialog";
import { useCanvasStore } from "@/store/canvasStore";
import { RENDER_MODEL } from "@/test/fixtures";

// The UI-level guard for the recurring "incomplete relation" bug (spec §2/§3/§7): the Create button
// stays disabled until a foreign-key field is chosen, so the canvas can never emit a relation
// without `foreignKeyFieldId`.

describe("RelationDialog (FK field is mandatory)", () => {
  afterEach(() => vi.restoreAllMocks());

  function setup() {
    useCanvasStore.setState({ model: RENDER_MODEL });
    const onClose = vi.fn();
    render(<RelationDialog pending={{ source: "tbl_orders001", target: "tbl_users0001" }} onClose={onClose} />);
    return { onClose };
  }

  const createBtn = () => screen.getByRole("button", { name: /create relation/i });

  it("defaults to creating a new FK field named after the target and enables Create", () => {
    setup();
    expect((screen.getByLabelText(/new foreign-key field name/i) as HTMLInputElement).value).toBe("users_id");
    expect(createBtn()).toBeEnabled();
  });

  it("disables Create when no FK field can be determined (empty new name)", () => {
    setup();
    fireEvent.change(screen.getByLabelText(/new foreign-key field name/i), { target: { value: "  " } });
    expect(createBtn()).toBeDisabled();
  });

  it("disables Create in 'existing field' mode until a field is selected", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: /existing field/i }));
    expect(createBtn()).toBeDisabled(); // the placeholder option has an empty value
    fireEvent.change(screen.getByLabelText(/existing foreign-key field/i), { target: { value: "fld_ouser" } });
    expect(createBtn()).toBeEnabled();
  });

  it("on confirm, calls connect with a complete relation request", () => {
    const connect = vi.fn();
    useCanvasStore.setState({ model: RENDER_MODEL, connect });
    const onClose = vi.fn();
    render(<RelationDialog pending={{ source: "tbl_orders001", target: "tbl_users0001" }} onClose={onClose} />);

    fireEvent.click(screen.getByRole("button", { name: /create relation/i }));
    expect(connect).toHaveBeenCalledWith(
      expect.objectContaining({
        fromTableId: "tbl_orders001",
        toTableId: "tbl_users0001",
        type: "one_to_many",
        newFkFieldName: "users_id",
      }),
    );
    expect(onClose).toHaveBeenCalled();
  });
});
