import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { diffIsEmpty, isDirty, useCanvasStore } from "./canvasStore";
import type { SchemaDoc } from "@/lib/schema";
import { RENDER_MODEL } from "@/test/fixtures";

// Milestone 3: the canvas drives the engine's diff + approval gate. These tests prove the gate's
// verdicts are reflected (not bypassed): a validation error blocks, a critical migration needs an
// explicit acknowledgement, and a healthy approve advances the engine state and makes the approved
// schema the new base (spec §2/§3/§5). The store computes none of this — it calls the engine.

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), { status });

// Per-test knobs for the simulated engine.
const cfg = {
  sessionValidateState: "validated" as "validated" | "draft",
  approve: (_ack: boolean): { body: unknown; status: number } => ({
    status: 200,
    body: { state: "approved", schemaVersion: "v1", checksum: "sha256:abc" },
  }),
};

// A faithful-enough diff: compare the table ids of `from` vs `to`, exactly as the engine would (so a
// move never adds ops and the diff genuinely collapses once the base is updated after approve).
function fakeDiff(body: string) {
  const { from, to } = JSON.parse(body || "{}");
  const fromIds: string[] = (from?.logical?.tables ?? []).map((t: { id: string }) => t.id);
  const toIds: string[] = (to?.logical?.tables ?? []).map((t: { id: string }) => t.id);
  const toSet = new Set(toIds);
  const fromSet = new Set(fromIds);
  const ops = [
    ...toIds.filter((id) => !fromSet.has(id)).map((id) => ({ op: "add_table", tableId: id })),
    ...fromIds.filter((id) => !toSet.has(id)).map((id) => ({ op: "drop_table", tableId: id })),
  ];
  return {
    operations: ops,
    colored: ops.map((o) => ({ color: o.op.startsWith("add") ? "green" : "red", text: `${o.op} ${o.tableId}` })),
    stats: { added: ops.filter((o) => o.op.startsWith("add")).length, removed: ops.filter((o) => o.op.startsWith("drop")).length, changed: 0, renamed: 0 },
    changelog: [],
    notes: [],
  };
}

function routedFetch() {
  return vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    const isSession = u.includes("/design/sessions");
    if (u.includes("/design/render")) return json(RENDER_MODEL);
    if (u.includes("/core/types")) return json({ types: [] });
    if (u.includes("/core/validate")) return json({ report: { valid: true, findings: [], summary: {} } });
    if (u.includes("/core/diff")) return json(fakeDiff(String(init?.body ?? "{}")));
    if (isSession && u.endsWith("/apply-suggestion")) return json({ state: "draft" });
    if (isSession && u.endsWith("/validate")) {
      const errors = cfg.sessionValidateState === "validated" ? 0 : 1;
      return json({ state: cfg.sessionValidateState, report: { summary: { error: errors } } });
    }
    if (isSession && u.endsWith("/submit")) return json({ state: "pending_approval" });
    if (isSession && u.endsWith("/approve")) {
      const ack = !!JSON.parse(String(init?.body ?? "{}")).acknowledgeCritical;
      const { status, body } = cfg.approve(ack);
      return json(body, status);
    }
    // GET a session's doc by id (fetchSessionDoc) — the `?sessionId=` load path (bug §7 test).
    if (isSession && /\/design\/sessions\/[^/]+$/.test(u) && (init?.method ?? "GET") === "GET") {
      return json({ schema_json: doc() });
    }
    if (isSession) return json({ sessionId: "ses_test01" }); // create
    return json({});
  });
}

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        { id: "tbl_users0001", name: "users", fields: [{ id: "fld_u", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }] },
        { id: "tbl_orders001", name: "orders", fields: [{ id: "fld_o", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }] },
      ],
      relations: [],
    },
  };
}

describe("canvasStore diff + approve (engine gate)", () => {
  beforeEach(async () => {
    cfg.sessionValidateState = "validated";
    cfg.approve = (_ack) => ({ status: 200, body: { state: "approved", schemaVersion: "v1", checksum: "sha256:abc" } });
    globalThis.fetch = routedFetch() as never;
    await useCanvasStore.getState().load({ schemaJson: doc() });
  });
  afterEach(() => vi.restoreAllMocks());

  it("starts with no diff against the freshly-loaded base", () => {
    expect(diffIsEmpty(useCanvasStore.getState().diff)).toBe(true);
  });

  it("reflects the engine's operation list after a schema edit", async () => {
    await useCanvasStore.getState().removeTable("tbl_orders001");
    expect(useCanvasStore.getState().diff?.operations).toHaveLength(1);
    expect(useCanvasStore.getState().diff?.operations[0].op).toBe("drop_table");
  });

  it("a table move does not enter the diff (layout is not a schema change, spec §5)", async () => {
    useCanvasStore.getState().commitPositions({ tbl_users0001: { x: 9, y: 9 } });
    await Promise.resolve();
    expect(diffIsEmpty(useCanvasStore.getState().diff)).toBe(true);
  });

  it("a healthy approve advances the gate and makes the approved schema the new base (spec §2)", async () => {
    await useCanvasStore.getState().removeTable("tbl_orders001");
    expect(isDirty(useCanvasStore.getState())).toBe(true);

    const res = await useCanvasStore.getState().runApprove(false);
    expect(res).toMatchObject({ status: "approved", schemaVersion: "v1" });

    const s = useCanvasStore.getState();
    expect(s.approved?.schemaVersion).toBe("v1");
    expect(isDirty(s)).toBe(false); // the approved doc is the new base
    expect(diffIsEmpty(s.diff)).toBe(true); // nothing pending against the new base
  });

  it("blocks approve when the engine validation is not green (spec §2)", async () => {
    cfg.sessionValidateState = "draft"; // gate's validate refuses to advance
    const res = await useCanvasStore.getState().runApprove(false);
    expect(res.status).toBe("validation_error");
    expect(useCanvasStore.getState().approved).toBeNull();
  });

  it("requires explicit acknowledgement for a critical migration (negative gate test, spec §3/§5)", async () => {
    cfg.approve = (ack) =>
      ack
        ? { status: 200, body: { state: "approved", schemaVersion: "v1", checksum: "sha256:abc" } }
        : {
            status: 409,
            body: {
              error: "gate_blocked",
              reason: "critical_migration_risk",
              blocking: [{ op: "drop_table", target: "tbl_orders001", level: "critical" }],
            },
          };

    const blocked = await useCanvasStore.getState().runApprove(false);
    expect(blocked.status).toBe("critical_risk");
    if (blocked.status === "critical_risk") expect(blocked.blocking).toHaveLength(1);
    expect(useCanvasStore.getState().approved).toBeNull();

    const ok = await useCanvasStore.getState().runApprove(true);
    expect(ok.status).toBe("approved");
    expect(useCanvasStore.getState().approved?.schemaVersion).toBe("v1");
  });

  it("persists the approved schema back to the loaded session so a reload keeps it (bug §7)", async () => {
    // Re-load the canvas from a real design session (the `?sessionId=` deployment path), not a bare
    // schemaJson — that is the case where edits used to vanish on reload, because the approve gate ran
    // on a throwaway baseline session and the *loaded* session's schema_doc was never updated.
    const fetchSpy = routedFetch();
    globalThis.fetch = fetchSpy as never;
    await useCanvasStore.getState().load({ sessionId: "ses_loaded01" });

    await useCanvasStore.getState().removeTable("tbl_orders001");
    fetchSpy.mockClear();

    const res = await useCanvasStore.getState().runApprove(false);
    expect(res.status).toBe("approved");

    // The working doc (minus the dropped table) must be applied back to the LOADED session id, so a
    // subsequent fetchSessionDoc returns the approved map — not the pre-edit one.
    const applyToLoaded = fetchSpy.mock.calls.find(
      ([url]) => String(url).includes("/design/sessions/ses_loaded01/apply-suggestion"),
    );
    expect(applyToLoaded).toBeTruthy();
    const persisted = JSON.parse(String((applyToLoaded![1] as RequestInit).body)).schema_json;
    expect(persisted.logical.tables.map((t: { id: string }) => t.id)).toEqual(["tbl_users0001"]);
  });
});
