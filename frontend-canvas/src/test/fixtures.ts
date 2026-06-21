import type { RenderModel } from "@/lib/types";

/** A resolved render model (the shape `/design/render` returns) used across the front-end tests. */
export const RENDER_MODEL: RenderModel = {
  meta: { name: "shop", databaseType: "postgres" },
  tables: [
    {
      id: "tbl_users0001",
      name: "users",
      comment: null,
      kind: "normal",
      fields: [
        { id: "fld_uid", name: "id", semanticType: "uuid", physicalType: "uuid", nullable: false, isPrimaryKey: true, isForeignKey: false, pii: false, sensitivity: null, enumId: null, comment: null },
        { id: "fld_email", name: "email", semanticType: "email", physicalType: "varchar(255)", nullable: false, isPrimaryKey: false, isForeignKey: false, pii: true, sensitivity: "medium", enumId: null, comment: null },
      ],
    },
    {
      id: "tbl_orders001",
      name: "orders",
      comment: null,
      kind: "normal",
      fields: [
        { id: "fld_oid", name: "id", semanticType: "uuid", physicalType: "uuid", nullable: false, isPrimaryKey: true, isForeignKey: false, pii: false, sensitivity: null, enumId: null, comment: null },
        { id: "fld_ouser", name: "user_id", semanticType: "foreign_key", physicalType: "uuid", nullable: false, isPrimaryKey: false, isForeignKey: true, pii: false, sensitivity: null, enumId: null, comment: null },
      ],
    },
  ],
  relations: [
    { id: "rel_order_usr", type: "one_to_many", fromTableId: "tbl_orders001", toTableId: "tbl_users0001", foreignKeyFieldId: "fld_ouser", onDelete: "cascade", onUpdate: null },
  ],
  enums: [],
  presentation: { nodes: [] },
  hasLayout: false,
};
