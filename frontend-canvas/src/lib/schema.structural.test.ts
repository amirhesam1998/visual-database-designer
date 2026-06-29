import { describe, expect, it } from "vitest";
import * as edit from "./schema";
import type { SchemaDoc } from "./schema";

const ID_RE = /^(tbl|fld|rel|enm|idx)_[0-9A-Za-z._-]{4,}$/;

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        {
          id: "tbl_users0001",
          name: "users",
          fields: [
            { id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
            { id: "fld_email0001", name: "email", semanticType: "email", nullable: false },
          ],
        },
      ],
      relations: [],
    },
    presentation: { nodes: [{ tableId: "tbl_users0001", x: 10, y: 20 }] },
  };
}

describe("duplicate table (C6 — structural, fresh Stable IDs)", () => {
  it("deep-copies with brand-new ids and a _copy name", () => {
    const { doc: next, tableId } = edit.duplicateTable(doc(), "tbl_users0001");
    expect(next.logical.tables).toHaveLength(2);
    const copy = next.logical.tables.find((t) => t.id === tableId)!;
    expect(copy.name).toBe("users_copy");
    expect(copy.id).toMatch(ID_RE);
    expect(copy.id).not.toBe("tbl_users0001");
    // Every field id is freshly minted — ids are never copied (AD-1).
    for (const f of copy.fields) expect(f.id).toMatch(ID_RE);
    expect(copy.fields.map((f) => f.id)).not.toContain("fld_uid000001");
    // Same structure, different identity.
    expect(copy.fields.map((f) => f.name)).toEqual(["id", "email"]);
  });

  it("avoids name collisions on repeated duplication", () => {
    const once = edit.duplicateTable(doc(), "tbl_users0001").doc;
    const twice = edit.duplicateTable(once, "tbl_users0001").doc;
    const names = twice.logical.tables.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length); // all unique
  });
});

describe("timestamps / soft-delete (C7 — real columns, not flags)", () => {
  it("adds created_at + updated_at as real datetime columns", () => {
    const next = edit.setTimestamps(doc(), "tbl_users0001", true);
    const fields = next.logical.tables[0].fields;
    const ts = fields.filter((f) => ["created_at", "updated_at"].includes(f.name));
    expect(ts).toHaveLength(2);
    expect(ts.every((f) => f.semanticType === "datetime")).toBe(true);
    expect(edit.tableHasTimestamps(next.logical.tables[0])).toBe(true);
  });

  it("removing timestamps deletes exactly those columns", () => {
    const on = edit.setTimestamps(doc(), "tbl_users0001", true);
    const off = edit.setTimestamps(on, "tbl_users0001", false);
    const names = off.logical.tables[0].fields.map((f) => f.name);
    expect(names).toEqual(["id", "email"]);
  });

  it("soft delete adds a nullable deleted_at column", () => {
    const next = edit.setSoftDelete(doc(), "tbl_users0001", true);
    const del = next.logical.tables[0].fields.find((f) => f.name === "deleted_at")!;
    expect(del.semanticType).toBe("datetime");
    expect(del.nullable).toBe(true);
  });
});

describe("reusable enums (engine logical.enums)", () => {
  it("creates, updates and attaches/detaches an enum", () => {
    const { doc: withEnum, enumId } = edit.addEnum(doc(), "order_status", ["open", "closed"]);
    expect(enumId).toMatch(ID_RE);
    expect(withEnum.logical.enums).toHaveLength(1);
    expect(withEnum.logical.enums![0].values.map((v) => v.value)).toEqual(["open", "closed"]);

    const renamed = edit.updateEnum(withEnum, enumId, { name: "status", values: ["a", "b", "c"] });
    expect(renamed.logical.enums![0].name).toBe("status");
    expect(renamed.logical.enums![0].values).toHaveLength(3);

    // attach to a field, then deleting the enum must detach it (no dangling ref)
    const attached = edit.updateField(renamed, "tbl_users0001", "fld_email0001", { enumId });
    expect(attached.logical.tables[0].fields[1].enumId).toBe(enumId);
    const removed = edit.removeEnum(attached, enumId);
    expect(removed.logical.enums).toHaveLength(0);
    expect(removed.logical.tables[0].fields[1].enumId).toBeNull();
  });
});

describe("explicit indexes (engine physical.indexes)", () => {
  it("adds, lists and removes an index; field removal prunes it", () => {
    const { doc: withIdx, indexId } = edit.addIndex(doc(), "tbl_users0001", { columns: ["fld_email0001"], unique: true });
    expect(indexId).toMatch(ID_RE);
    expect(edit.indexesForTable(withIdx, "tbl_users0001")).toHaveLength(1);
    expect(withIdx.physical!.indexes![0].unique).toBe(true);

    // Removing the indexed field prunes the (now-empty) index.
    const fieldGone = edit.removeField(withIdx, "tbl_users0001", "fld_email0001");
    expect(edit.indexesForTable(fieldGone, "tbl_users0001")).toHaveLength(0);

    const idxGone = edit.removeIndex(withIdx, indexId);
    expect(edit.indexesForTable(idxGone, "tbl_users0001")).toHaveLength(0);
  });

  it("removing a table drops its indexes", () => {
    const { doc: withIdx } = edit.addIndex(doc(), "tbl_users0001", { columns: ["fld_email0001"] });
    const gone = edit.removeTable(withIdx, "tbl_users0001");
    expect(gone.physical?.indexes ?? []).toHaveLength(0);
  });
});

describe("signature ignores presentation but tracks the new layers", () => {
  it("enum and index changes are schema changes", () => {
    const base = edit.schemaSignature(doc());
    expect(edit.schemaSignature(edit.addEnum(doc(), "e", ["x"]).doc)).not.toBe(base);
    expect(edit.schemaSignature(edit.addIndex(doc(), "tbl_users0001", { columns: ["fld_email0001"] }).doc)).not.toBe(base);
  });
});
