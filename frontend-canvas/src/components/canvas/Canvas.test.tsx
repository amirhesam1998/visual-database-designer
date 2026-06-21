import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "reactflow";
import { Canvas } from "./Canvas";
import { RENDER_MODEL } from "@/test/fixtures";

function renderCanvas() {
  return render(
    <ReactFlowProvider>
      <div style={{ width: 800, height: 600 }}>
        <Canvas model={RENDER_MODEL} />
      </div>
    </ReactFlowProvider>,
  );
}

describe("Canvas (read-only render)", () => {
  it("renders a node for every table", () => {
    renderCanvas();
    expect(screen.getByTestId("table-node-users")).toBeInTheDocument();
    expect(screen.getByTestId("table-node-orders")).toBeInTheDocument();
  });

  it("renders fields with their resolved physical type — the FK shows uuid, not an integer", () => {
    renderCanvas();
    const orders = screen.getByTestId("table-node-orders");
    const userIdRow = within(orders).getByTestId("field-user_id");
    expect(within(userIdRow).getByText("uuid")).toBeInTheDocument();
  });

  it("marks the primary key and a sensitive field", () => {
    renderCanvas();
    const users = screen.getByTestId("table-node-users");
    expect(within(users).getByLabelText("primary key")).toBeInTheDocument();
    expect(within(users).getByLabelText("sensitive field")).toBeInTheDocument();
  });

  it("draws a directional relation edge with its cardinality label", () => {
    renderCanvas();
    // React Flow renders the edge path and the cardinality pill into the DOM.
    expect(screen.getByText("1 — ∞")).toBeInTheDocument();
    expect(document.querySelector(".react-flow__edge")).toBeTruthy();
  });
});
