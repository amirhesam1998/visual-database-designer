// A small, self-contained schema_json used when the canvas is opened without a `?sessionId=` (so the
// page renders something real out of the box). It is sent to `POST /design/render`, so the *engine*
// resolves the types — note `orders.user_id` is a `foreign_key` onto a uuid PK and comes back `uuid`.
// Deliberately ships with NO `presentation` layer to exercise the auto-layout path (spec §3).
export const SAMPLE_SCHEMA_JSON = {
  formatVersion: "1.0.0",
  meta: { name: "shop", databaseType: "postgres", defaultDriver: "postgres" },
  logical: {
    tables: [
      {
        id: "tbl_users0001",
        name: "users",
        kind: "normal",
        fields: [
          { id: "fld_uid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_uemail001", name: "email", semanticType: "email", nullable: false },
          { id: "fld_upass0001", name: "password", semanticType: "password", nullable: false },
          { id: "fld_ucreated1", name: "created_at", semanticType: "datetime", nullable: false },
        ],
      },
      {
        id: "tbl_orders001",
        name: "orders",
        kind: "normal",
        fields: [
          { id: "fld_oid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_ouser0001", name: "user_id", semanticType: "foreign_key", nullable: false },
          { id: "fld_ototal001", name: "total", semanticType: "money", nullable: false },
          { id: "fld_ostatus01", name: "status", semanticType: "string", nullable: false },
        ],
      },
      {
        id: "tbl_products01",
        name: "products",
        kind: "normal",
        fields: [
          { id: "fld_pid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_pname0001", name: "name", semanticType: "string", nullable: false },
          { id: "fld_pprice001", name: "price", semanticType: "money", nullable: false },
        ],
      },
      {
        id: "tbl_orditems1",
        name: "order_items",
        kind: "pivot",
        fields: [
          { id: "fld_iid000001", name: "id", semanticType: "uuid", isPrimaryKey: true, nullable: false },
          { id: "fld_iorder001", name: "order_id", semanticType: "foreign_key", nullable: false },
          { id: "fld_iprod0001", name: "product_id", semanticType: "foreign_key", nullable: false },
          { id: "fld_iqty00001", name: "quantity", semanticType: "integer", nullable: false },
        ],
      },
    ],
    relations: [
      {
        id: "rel_order_usr",
        name: "belongsTo",
        type: "one_to_many",
        fromTableId: "tbl_orders001",
        toTableId: "tbl_users0001",
        foreignKeyFieldId: "fld_ouser0001",
        onDelete: "cascade",
      },
      {
        id: "rel_item_ord",
        type: "one_to_many",
        fromTableId: "tbl_orditems1",
        toTableId: "tbl_orders001",
        foreignKeyFieldId: "fld_iorder001",
        onDelete: "cascade",
      },
      {
        id: "rel_item_prod",
        type: "one_to_many",
        fromTableId: "tbl_orditems1",
        toTableId: "tbl_products01",
        foreignKeyFieldId: "fld_iprod0001",
        onDelete: "restrict",
      },
    ],
  },
};
