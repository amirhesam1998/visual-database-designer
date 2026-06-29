import { beforeEach, describe, expect, it } from "vitest";
import { useSavedConnections } from "./savedConnections";

// Saved connections (import-fixes milestone §3). The security-critical guarantee: a password is NEVER
// persisted — not in the store, not in localStorage. These tests pin that, plus save/remove/reload.

const KEY = "vdb.connections.v1";

beforeEach(() => {
  localStorage.clear();
  useSavedConnections.setState({ connections: [] });
});

describe("savedConnections", () => {
  it("saves only the non-secret parts and drops any password", () => {
    const saved = useSavedConnections.getState().save({
      label: "prod", driver: "mysql", host: "db.example.com", port: "3306",
      database: "app", user: "root", password: "super-secret",
    });
    expect(saved).toBeTruthy();
    expect(saved as unknown as Record<string, unknown>).not.toHaveProperty("password");
    expect(saved).toMatchObject({ label: "prod", driver: "mysql", host: "db.example.com", user: "root" });
  });

  it("never writes a password into localStorage", () => {
    useSavedConnections.getState().save({
      label: "x", host: "h", database: "d", user: "u", password: "leaky-password-123",
    });
    const raw = localStorage.getItem(KEY) ?? "";
    expect(raw).not.toContain("leaky-password-123");
    expect(raw).not.toContain("password");
  });

  it("persists across a reload (store re-read from localStorage)", () => {
    const s = useSavedConnections.getState().save({ label: "a", host: "h", database: "d", user: "u" });
    useSavedConnections.setState({ connections: [] });        // simulate a fresh mount
    useSavedConnections.getState().reload();
    expect(useSavedConnections.getState().connections.map((c) => c.id)).toContain(s!.id);
  });

  it("removes a connection", () => {
    const s = useSavedConnections.getState().save({ label: "a", host: "h", database: "d", user: "u" })!;
    useSavedConnections.getState().remove(s.id);
    expect(useSavedConnections.getState().connections).toHaveLength(0);
    expect(localStorage.getItem(KEY)).toBe("[]");
  });

  it("updates in place when saving with an existing id", () => {
    const s = useSavedConnections.getState().save({ label: "a", host: "h1", database: "d", user: "u" })!;
    useSavedConnections.getState().save({ id: s.id, label: "a2", host: "h2", database: "d", user: "u" });
    const list = useSavedConnections.getState().connections;
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ id: s.id, label: "a2", host: "h2" });
  });
});
