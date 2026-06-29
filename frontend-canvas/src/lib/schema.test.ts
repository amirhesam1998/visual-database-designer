import { describe, expect, it } from "vitest";
import * as edit from "./schema";
import type { SchemaDoc } from "./schema";

const ID_RE = /^(tbl|fld|rel|enm)_[0-9A-Za-z._-]{4,}$/;

function doc(): SchemaDoc {
  return {
    formatVersion: "1.0.0",
    logical: {
      tables: [
        {
          id: "tbl_users0001",
          name: "users",
          fields: [{ id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false }],
        },
        {
          id: "tbl_orders001",
          name: "orders",
          fields: [
            { id: "fld_oid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
            { id: "fld_ouser0001", name: "user_id", semanticType: "foreign_key", nullable: false },
          ],
        },
      ],
      relations: [
        {
          id: "rel_order_usr",
          type: "one_to_many",
          fromTableId: "tbl_orders001",
          toTableId: "tbl_users0001",
          foreignKeyFieldId: "fld_ouser0001",
        },
      ],
    },
  };
}

describe("schema mutations (structural only, never decide validity)", () => {
  it("generates Stable IDs that match the format pattern (AD-1)", () => {
    for (const p of ["tbl", "fld", "rel"] as const) expect(edit.genId(p)).toMatch(ID_RE);
  });

  it("addField appends a field and never rewrites existing ids", () => {
    const before = doc();
    const { doc: after, fieldId } = edit.addField(before, "tbl_users0001", { name: "email", semanticType: "email" });
    expect(fieldId).toMatch(ID_RE);
    const users = after.logical.tables.find((t) => t.id === "tbl_users0001")!;
    expect(users.fields.map((f) => f.name)).toEqual(["id", "email"]);
    // existing entity ids are untouched
    expect(before.logical.tables[0].fields[0].id).toBe(after.logical.tables[0].fields[0].id);
  });

  it("updateField changes the semantic type but keeps the field id (an edit never changes id)", () => {
    const after = edit.updateField(doc(), "tbl_orders001", "fld_oid000001", { semanticType: "big_integer" });
    const f = after.logical.tables[1].fields[0];
    expect(f.id).toBe("fld_oid000001");
    expect(f.semanticType).toBe("big_integer");
  });

  it("removeField drops any relation that used it as the foreign key (no dangling/incomplete relation)", () => {
    const after = edit.removeField(doc(), "tbl_orders001", "fld_ouser0001");
    expect(after.logical.tables[1].fields.find((f) => f.id === "fld_ouser0001")).toBeUndefined();
    expect(after.logical.relations).toHaveLength(0);
  });

  it("removeTable cascades to its relations and its presentation node", () => {
    const withLayout = edit.setPositions(doc(), {
      tbl_users0001: { x: 1, y: 2 },
      tbl_orders001: { x: 3, y: 4 },
    });
    const after = edit.removeTable(withLayout, "tbl_users0001");
    expect(after.logical.tables.map((t) => t.id)).toEqual(["tbl_orders001"]);
    expect(after.logical.relations).toHaveLength(0); // the relation pointed at users
    expect(edit.presentationNodes(after).map((n) => n.tableId)).toEqual(["tbl_orders001"]);
  });

  it("addRelation always carries an explicit foreignKeyFieldId", () => {
    const { doc: after } = edit.addRelation(doc(), {
      type: "one_to_many",
      fromTableId: "tbl_orders001",
      toTableId: "tbl_users0001",
      foreignKeyFieldId: "fld_ouser0001",
    });
    expect(after.logical.relations).toHaveLength(2);
    expect(after.logical.relations!.every((r) => !!r.foreignKeyFieldId)).toBe(true);
  });

  it("a table move is NOT a schema change — schemaSignature ignores presentation (spec §4/§5)", () => {
    const base = doc();
    const moved = edit.setPositions(base, { tbl_users0001: { x: 999, y: 999 } });
    expect(edit.schemaSignature(moved)).toBe(edit.schemaSignature(base));

    const edited = edit.addField(base, "tbl_users0001", { name: "x", semanticType: "string" }).doc;
    expect(edit.schemaSignature(edited)).not.toBe(edit.schemaSignature(base));
  });
});
