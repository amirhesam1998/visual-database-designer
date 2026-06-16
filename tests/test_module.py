from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.comparators import SchemaComparator
from app.designer import SchemaDesigner
from app.exporters import (
    LaravelMigrationExporter,
    MermaidExporter,
    OpenAPIExporter,
    PrismaExporter,
    SQLExporter,
    export_one,
)
from app.generators import (
    export_framework_schema,
    generate_crud,
    generate_model,
    supported_frameworks,
)
from app.module import VisualDatabaseDesignerModule, app
from app.parsers import LaravelMigrationParser, SQLParser
from app.presets import field_presets
from app.schema_model import (
    DatabaseSchema,
    EnumDef,
    FieldType,
    Index,
    Relation,
    RelationType,
    SchemaField,
    Table,
)
from app.templates import build_template_schema
from app.validators import SchemaValidator
from app.versioning import compare_schemas, diff_to_migration, empty_schema

FIXTURES = Path(__file__).parent / "fixtures"


class DummyCtx:
    def __init__(self):
        self.logs: list[str] = []
        self.settings: dict = {}
        self.llm = None
        self.source = None

    def log(self, msg: str) -> None:
        self.logs.append(msg)


def make_schema() -> DatabaseSchema:
    users = Table(
        name="users",
        fields=[
            SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False),
            SchemaField(name="email", type=FieldType.VARCHAR, length=255, unique=True, indexed=True, nullable=False),
        ],
    )
    orders = Table(
        name="orders",
        fields=[
            SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False),
            SchemaField(name="user_id", type=FieldType.FOREIGN_ID, indexed=True, nullable=False),
            SchemaField(name="total", type=FieldType.DECIMAL, precision=10, scale=2, nullable=False),
        ],
        relations=[Relation(from_table="orders", from_field="user_id", to_table="users",
                            to_field="id", type=RelationType.MANY_TO_ONE)],
    )
    return DatabaseSchema(id="test", driver="postgresql", tables=[users, orders])


# --------------------------------------------------------------------------
# Schema model
# --------------------------------------------------------------------------


def test_schema_json_roundtrip():
    schema = make_schema()
    data = schema.model_dump(mode="json")
    restored = DatabaseSchema.model_validate(data)
    assert restored.tables[0].name == "users"
    assert restored.tables[1].relations[0].to_table == "users"
    assert restored.all_relations()[0].from_field == "user_id"


def test_fieldtype_coerce_aliases():
    assert FieldType.coerce("INT") == FieldType.INTEGER
    assert FieldType.coerce("jsonb") == FieldType.JSON
    assert FieldType.coerce("something-weird") == FieldType.VARCHAR


# --------------------------------------------------------------------------
# Templates (heuristic, offline)
# --------------------------------------------------------------------------


def test_template_ecommerce_for_clothing_store():
    schema = build_template_schema("Build a clothing store")
    names = {t.name for t in schema.tables}
    assert len(schema.tables) >= 5
    assert "products" in names
    assert "orders" in names


def test_template_blog():
    schema = build_template_schema("A blog with articles and comments")
    assert "posts" in {t.name for t in schema.tables}


def test_template_generic():
    schema = build_template_schema("a tool to record notes")
    assert schema.metadata.get("domain") == "generic"
    assert any(t.name == "users" for t in schema.tables)
    assert len(schema.tables) == 2


def test_pluralize():
    from app.templates import _pluralize

    assert _pluralize("category") == "categories"
    assert _pluralize("box") == "boxes"
    assert _pluralize("widget") == "widgets"


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


def test_validation_passes_for_good_schema():
    result = SchemaValidator(make_schema()).validate()
    assert result["valid"] is True
    assert result["errors"] == []


def test_validation_flags_missing_primary_key():
    schema = DatabaseSchema(tables=[Table(name="t", fields=[SchemaField(name="x", type=FieldType.INTEGER)])])
    result = SchemaValidator(schema).validate()
    assert result["valid"] is False
    assert any("primary key" in e for e in result["errors"])


def test_validation_flags_bad_relation_target():
    schema = make_schema()
    schema.tables[1].relations[0].to_table = "ghost"
    result = SchemaValidator(schema).validate()
    assert result["valid"] is False
    assert any("ghost" in e for e in result["errors"])


def test_validation_warns_unindexed_fk():
    schema = make_schema()
    schema.tables[1].fields[1].indexed = False  # user_id no longer indexed
    result = SchemaValidator(schema).validate()
    assert any("user_id" in w for w in result["warnings"])


# --------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------


def test_export_sql():
    sql = SQLExporter(make_schema()).export()
    assert "CREATE TABLE users" in sql
    assert "PRIMARY KEY" in sql
    assert "BIGSERIAL" in sql  # postgres auto-increment
    assert "FOREIGN KEY (user_id) REFERENCES users(id)" in sql


def test_export_laravel_migration():
    code = LaravelMigrationExporter(make_schema()).export()
    assert "Schema::create('users'" in code
    assert "$table->id('id');" in code
    assert "Schema::dropIfExists('orders');" in code


def test_export_prisma():
    prisma = PrismaExporter(make_schema()).export()
    assert "model Users {" in prisma
    assert "@id" in prisma
    assert "@@map(\"users\")" in prisma


def test_export_mermaid():
    mermaid = MermaidExporter(make_schema()).export()
    assert mermaid.startswith("erDiagram")
    assert "orders" in mermaid and "users" in mermaid


def test_export_openapi_is_valid_json():
    spec = json.loads(OpenAPIExporter(make_schema()).export())
    assert spec["openapi"].startswith("3.")
    assert "Users" in spec["components"]["schemas"]
    assert "/users" in spec["paths"]


# --------------------------------------------------------------------------
# Parsers (import)
# --------------------------------------------------------------------------


def test_import_sql_dump():
    sql = (FIXTURES / "sample_dump.sql").read_text()
    schema = SQLParser().parse(sql)
    assert len(schema.tables) == 2
    users = schema.table("users")
    assert users is not None
    assert users.field("id").primary_key is True
    assert users.field("email").unique is True
    assert users.field("email").length == 255


def test_import_laravel_migration():
    code = """
    Schema::create('posts', function (Blueprint $table) {
        $table->id();
        $table->string('title');
        $table->text('body')->nullable();
        $table->timestamps();
    });
    """
    schema = LaravelMigrationParser().parse(code)
    posts = schema.table("posts")
    assert posts is not None
    assert posts.field("id").primary_key is True
    assert posts.field("title").type == FieldType.VARCHAR
    assert posts.timestamps is True


# --------------------------------------------------------------------------
# Comparator
# --------------------------------------------------------------------------


def test_comparator_detects_changes():
    old = make_schema()
    new = make_schema()
    new.tables[0].fields.append(SchemaField(name="name", type=FieldType.VARCHAR))
    new.tables.append(Table(name="payments", fields=[
        SchemaField(name="id", type=FieldType.BIGINT, primary_key=True)]))
    diff = SchemaComparator(old, new).diff()
    assert "payments" in diff["added_tables"]
    assert any(c["table"] == "users" and "name" in c["added_fields"] for c in diff["changed_tables"])


# --------------------------------------------------------------------------
# Designer engine — e2e (no LLM)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_designer_clothing_store_offline():
    designer = SchemaDesigner(None, settings={})
    result = await designer.design("Build a clothing store", ctx=DummyCtx())
    names = {t.name for t in result.tables}
    assert len(result.tables) >= 5
    assert "products" in names and "orders" in names
    assert result.validation.valid is True
    assert "CREATE TABLE" in result.exports.sql
    assert result.exports.prisma and result.exports.mermaid and result.exports.migration
    assert result.relations  # relations flattened to top level


@pytest.mark.asyncio
async def test_designer_import_existing_database():
    sql = (FIXTURES / "sample_dump.sql").read_text()
    designer = SchemaDesigner(None, settings={"database_type": "sql"})
    result = await designer.design("", existing_database=sql, ctx=DummyCtx())
    assert {t.name for t in result.tables} == {"users", "orders"}


@pytest.mark.asyncio
async def test_designer_uses_llm(monkeypatch):
    async def fake_raw(self, *, system, user, provider=None, model=None, temperature=None):
        if "database architect" in system:
            return json.dumps({
                "tables": [
                    {"name": "widgets", "fields": [
                        {"name": "id", "type": "bigint", "primary_key": True, "auto_increment": True,
                         "nullable": False},
                        {"name": "sku", "type": "varchar", "length": 64, "unique": True, "nullable": False},
                    ]},
                    {"name": "owners", "fields": [
                        {"name": "id", "type": "bigint", "primary_key": True, "auto_increment": True,
                         "nullable": False},
                    ]},
                ],
                "relations": [
                    {"from_table": "widgets", "from_field": "owner_id", "to_table": "owners",
                     "to_field": "id", "type": "many_to_one"},
                ],
            })
        if "database reviewer" in system:
            return json.dumps({"suggestions": ["Add an index on widgets.sku."]})
        return "{}"

    from aiarch_module_sdk.envelopes import LLMPort
    from aiarch_module_sdk.llm_client import LLMClient

    monkeypatch.setattr(LLMClient, "_raw_complete", fake_raw)
    llm = LLMClient(LLMPort(gateway_url="http://gw", token="t", default_model="m"))

    designer = SchemaDesigner(llm, settings={})
    result = await designer.design("anything", ctx=DummyCtx())
    names = {t.name for t in result.tables}
    assert "widgets" in names and "owners" in names
    assert any("widgets.sku" in s for s in result.suggestions)
    # the FK relation the LLM declared is attached to the owning table + flattened to top level
    assert any(r.from_table == "widgets" and r.to_table == "owners" for r in result.relations)


# --------------------------------------------------------------------------
# HTTP contract — /manifest, /health, /run
# --------------------------------------------------------------------------


def test_manifest():
    m = TestClient(app).get("/manifest").json()
    assert m["name"] == "visual_database_designer"
    assert m["produces"] == "database_schema"
    assert m["type"] == "generation"
    assert set(m["modes"]) == {"greenfield", "brownfield"}
    assert m["needs"]["llm"] is True
    assert "output_schema" in m


def test_health():
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["module"] == "visual_database_designer"


def test_run_greenfield_from_inputs():
    payload = {
        "request_id": "r1",
        "project_id": "p1",
        "mode": "greenfield",
        "inputs": {"feature_request": "Build a clothing store"},
        "context": {"llm": None, "source": None, "knowledge": None},
        "settings": {},
    }
    resp = TestClient(app).post("/run", json=payload).json()
    assert resp["status"] == "completed", resp
    assert resp["output_key"] == "database_schema"
    schema = resp["output"]
    assert schema["validation"]["valid"] is True
    assert schema["exports"]["sql"].startswith("CREATE TABLE")


def test_run_feature_request_from_settings():
    payload = {
        "request_id": "r2",
        "project_id": "p2",
        "mode": "brownfield",
        "inputs": {},
        "context": {"llm": None, "source": None, "knowledge": None},
        "settings": {"feature_request": "A blog with posts"},
    }
    resp = TestClient(app).post("/run", json=payload).json()
    assert resp["status"] == "completed", resp
    assert any(t["name"] == "posts" for t in resp["output"]["tables"])


# --------------------------------------------------------------------------
# Interactive routes — /design, /validate, /export, /import
# --------------------------------------------------------------------------


def test_interactive_design_and_export():
    client = TestClient(app)
    design = client.post("/design", json={"feature_request": "Build a clothing store"}).json()
    schema = design["database_schema"]
    assert schema["tables"]

    validation = client.post("/validate", json={"schema": schema}).json()
    assert validation["validation"]["valid"] is True

    export = client.post("/export", json={"schema": schema, "type": "mermaid"}).json()
    assert export["export_type"] == "mermaid"
    assert export["content"].startswith("erDiagram")


def test_interactive_import():
    client = TestClient(app)
    sql = (FIXTURES / "sample_dump.sql").read_text()
    resp = client.post("/import", json={"type": "sql", "data": sql}).json()
    assert {t["name"] for t in resp["database_schema"]["tables"]} == {"users", "orders"}


def test_export_unknown_type_returns_400():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/export", json={"schema": schema, "type": "cobol"})
    assert resp.status_code == 400


def test_module_signature_direct():
    mod = VisualDatabaseDesignerModule()
    assert mod.manifest.produces == "database_schema"
    assert mod.manifest.consumes == []


# --------------------------------------------------------------------------
# Phase 6F enhancements — model generation (#21)
# --------------------------------------------------------------------------


def make_users_with_password() -> DatabaseSchema:
    users = Table(
        name="users",
        soft_delete=True,
        fields=[
            SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False),
            SchemaField(name="email", type=FieldType.VARCHAR, length=255, unique=True, nullable=False),
            SchemaField(name="name", type=FieldType.VARCHAR, length=255, nullable=False),
            SchemaField(name="password", type=FieldType.VARCHAR, length=255, nullable=False),
            SchemaField(name="is_active", type=FieldType.BOOLEAN, default=True, nullable=False),
        ],
    )
    return DatabaseSchema(id="auth", driver="postgresql", tables=[users])


def test_generate_laravel_model():
    code = generate_model(make_schema(), "laravel", "users")
    assert "class User extends Model" in code
    assert "protected $table = 'users';" in code
    assert "'email'," in code  # fillable
    assert "public function orders()" in code  # hasMany Order
    assert "return $this->hasMany(Order::class);" in code


def test_generate_laravel_model_hides_password_and_casts():
    code = generate_model(make_users_with_password(), "laravel", "users")
    assert "use Illuminate\\Database\\Eloquent\\SoftDeletes;" in code
    assert "protected $hidden = [" in code
    assert "'password'," in code
    assert "'is_active' => 'boolean'," in code


def test_generate_typeorm_model():
    code = generate_model(make_schema(), "typeorm", "orders")
    assert "@Entity('orders')" in code
    assert "export class Order {" in code
    assert "@PrimaryGeneratedColumn()" in code
    assert "@ManyToOne(() => User" in code  # belongs to users


def test_generate_sqlalchemy_model():
    code = generate_model(make_schema(), "sqlalchemy", "users")
    assert "class User(Base):" in code
    assert "__tablename__ = 'users'" in code
    assert "Column(BigInteger, primary_key=True)" in code
    assert "relationship(" in code


def test_generate_django_model():
    code = generate_model(make_schema(), "django", "orders")
    assert "class Order(models.Model):" in code
    assert "db_table = 'orders'" in code
    assert "models.ForeignKey(" in code  # user_id


def test_generate_prisma_model():
    code = generate_model(make_schema(), "prisma", "users")
    assert "model User {" in code
    assert "@id" in code
    assert '@@map("users")' in code


def test_generate_model_unknown_framework_raises():
    with pytest.raises(ValueError):
        generate_model(make_schema(), "cobol", "users")


def test_generate_model_unknown_table_raises():
    with pytest.raises(ValueError):
        generate_model(make_schema(), "laravel", "ghost")


# --------------------------------------------------------------------------
# Phase 6F enhancements — CRUD controllers (#22)
# --------------------------------------------------------------------------


def test_generate_laravel_crud():
    code = generate_crud(make_users_with_password(), "laravel", "users")
    assert "class UserController extends Controller" in code
    assert "public function index()" in code
    assert "User::paginate(15)" in code
    assert "$request->validate([" in code
    assert "'email' => 'required|email'" in code
    assert "$validated['password'] = bcrypt($validated['password']);" in code
    assert "return response()->json(null, 204);" in code  # destroy


def test_generate_express_crud():
    code = generate_crud(make_users_with_password(), "express", "users")
    assert "import { Router, Request, Response } from 'express';" in code
    assert "import bcrypt from 'bcrypt';" in code
    assert "router.post('/'," in code
    assert "bcrypt.hash(password, 10)" in code
    assert "res.status(201).json(item);" in code


def test_generate_django_crud():
    code = generate_crud(make_schema(), "django", "users")
    assert "class UserViewSet(viewsets.ModelViewSet):" in code
    assert "queryset = User.objects.all()" in code
    assert "status.HTTP_201_CREATED" in code


def test_generate_crud_method_subset():
    code = generate_crud(make_schema(), "laravel", "users", methods=["index", "show"])
    assert "public function index()" in code
    assert "public function show($id)" in code
    assert "public function store(" not in code
    assert "public function destroy(" not in code


# --------------------------------------------------------------------------
# Phase 6F enhancements — framework full-schema export (#4)
# --------------------------------------------------------------------------


def test_export_framework_django():
    code = export_framework_schema(make_schema(), "django")
    assert "from django.db import models" in code
    assert "class User(models.Model):" in code
    assert "class Order(models.Model):" in code


def test_export_framework_sequelize():
    code = export_framework_schema(make_schema(), "sequelize")
    assert "extends Model" in code
    assert "DataTypes.BIGINT" in code
    assert "tableName: 'users'" in code


def test_export_framework_typeorm_and_sqlalchemy():
    assert "@Entity('users')" in export_framework_schema(make_schema(), "typeorm")
    assert "__tablename__ = 'orders'" in export_framework_schema(make_schema(), "sqlalchemy")


def test_export_framework_unknown_raises():
    with pytest.raises(ValueError):
        export_framework_schema(make_schema(), "cobol")


def test_supported_frameworks_shape():
    fw = supported_frameworks()
    assert "django" in fw["export"] and "sql" in fw["export"]
    assert set(fw["model"]) >= {"laravel", "typeorm", "sqlalchemy", "django", "prisma"}
    assert set(fw["crud"]) >= {"laravel", "express", "django"}
    assert fw["crud_methods"] == ["index", "show", "store", "update", "destroy"]


# --------------------------------------------------------------------------
# Phase 6F enhancements — HTTP routes
# --------------------------------------------------------------------------


def test_http_generate_model():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/generate/model", json={"schema": schema, "framework": "laravel", "table": "users"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["framework"] == "laravel"
    assert "class User extends Model" in body["content"]


def test_http_generate_crud():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/generate/crud", json={"schema": schema, "framework": "express", "table": "users"})
    assert resp.status_code == 200
    assert "export default router;" in resp.json()["content"]


def test_http_export_framework_schema():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/export", json={"schema": schema, "type": "django"})
    assert resp.status_code == 200
    assert "models.Model" in resp.json()["content"]


def test_http_frameworks_endpoint():
    body = TestClient(app).get("/frameworks").json()
    assert "laravel" in body["model"]
    assert "django" in body["export"]


def test_http_generate_model_unknown_framework_returns_400():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/generate/model", json={"schema": schema, "framework": "cobol", "table": "users"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Phase 6F enhancements — Phase 2: table groups (#6) + field presets (#12)
# --------------------------------------------------------------------------


def test_table_group_roundtrips():
    schema = make_schema()
    schema.tables[0].group = "User Management"
    data = schema.model_dump(mode="json")
    restored = DatabaseSchema.model_validate(data)
    assert restored.tables[0].group == "User Management"
    # group is canvas-only metadata — exporters must ignore it and still work
    assert "CREATE TABLE users" in SQLExporter(restored).export()


def test_table_group_defaults_none():
    assert make_schema().tables[0].group is None


def test_field_presets_content():
    presets = field_presets()
    by_name = {p["field"]["name"]: p["field"] for p in presets}
    assert by_name["id"]["primary_key"] is True
    assert by_name["email"]["unique"] is True
    assert by_name["created_at"]["type"] == "timestamp"
    assert by_name["status"]["type"] == "enum" and by_name["status"]["values"]
    # every preset carries a label + a valid FieldType
    for p in presets:
        assert p["label"]
        assert FieldType.coerce(p["field"]["type"]) == FieldType(p["field"]["type"])


def test_field_presets_are_constructible_fields():
    # each preset payload must validate as a real SchemaField
    for p in field_presets():
        SchemaField.model_validate(p["field"])


def test_http_field_presets_endpoint():
    body = TestClient(app).get("/field-presets").json()
    names = {p["field"]["name"] for p in body["presets"]}
    assert {"id", "email", "created_at", "user_id"} <= names


# --------------------------------------------------------------------------
# Phase 6F enhancements — Phase 3: enums (#13), composite keys (#14),
# index management (#15), comments/docs (#16), versioning (#9)
# --------------------------------------------------------------------------


def make_composite_schema() -> DatabaseSchema:
    pivot = Table(
        name="role_user",
        timestamps=False,
        fields=[
            SchemaField(name="user_id", type=FieldType.BIGINT, primary_key=True, nullable=False),
            SchemaField(name="role_id", type=FieldType.BIGINT, primary_key=True, nullable=False),
        ],
    )
    return DatabaseSchema(id="pivot", tables=[pivot])


def test_enum_materialization():
    schema = DatabaseSchema(
        enums=[EnumDef(name="order_status", values=["pending", "paid", "shipped"])],
        tables=[Table(name="orders", fields=[
            SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False),
            SchemaField(name="status", type=FieldType.ENUM, enum_ref="order_status", nullable=False),
        ])],
    )
    schema.materialize_enums()
    status = schema.table("orders").field("status")
    assert status.values == ["pending", "paid", "shipped"]
    # and the SQL exporter renders the resolved CHECK constraint
    sql = SQLExporter(schema).export()
    assert "status IN ('pending', 'paid', 'shipped')" in sql


def test_composite_primary_key_sql_laravel_prisma():
    schema = make_composite_schema()
    sql = SQLExporter(schema).export()
    assert "PRIMARY KEY (user_id, role_id)" in sql
    laravel = LaravelMigrationExporter(schema).export()
    assert "$table->primary(['user_id', 'role_id']);" in laravel
    prisma = PrismaExporter(schema).export()
    assert "@@id([user_id, role_id])" in prisma
    assert "@id" not in prisma.replace("@@id", "")  # composite → no per-field @id


def test_index_management_exports():
    schema = make_schema()
    schema.tables[0].indexes = [
        Index(columns=["email"], unique=True),
        Index(columns=["email", "id"]),
        Index(columns=["email"], type="fulltext"),
    ]
    sql = SQLExporter(schema).export()
    assert "CREATE UNIQUE INDEX users_email_uq ON users (email);" in sql
    assert "CREATE INDEX users_email_id_idx ON users (email, id);" in sql
    assert "USING GIN" in sql
    laravel = LaravelMigrationExporter(schema).export()
    assert "$table->unique(['email']);" in laravel
    assert "$table->index(['email', 'id']);" in laravel
    assert "$table->fullText(['email']);" in laravel
    prisma = PrismaExporter(schema).export()
    assert "@@unique([email])" in prisma
    assert "@@index([email, id])" in prisma


def test_comments_in_sql_and_laravel():
    schema = make_schema()
    schema.tables[0].description = "Application users"
    schema.tables[0].field("email").description = "Unique login email"
    sql = SQLExporter(schema).export()
    assert "COMMENT ON TABLE users IS 'Application users';" in sql
    assert "COMMENT ON COLUMN users.email IS 'Unique login email';" in sql
    laravel = LaravelMigrationExporter(schema).export()
    assert "->comment('Unique login email')" in laravel
    assert "$table->comment('Application users');" in laravel


def test_markdown_doc_export():
    schema = make_schema()
    schema.enums = [EnumDef(name="status", values=["a", "b"])]
    schema.tables[0].description = "Users table"
    md = export_one(schema, "markdown")
    assert md.startswith("# Data Dictionary")
    assert "## users" in md
    assert "| Column | Type | Constraints | Description |" in md
    assert "## Enums" in md and "status" in md


def test_export_all_includes_markdown():
    from app.exporters import export_all

    exports = export_all(make_schema())
    assert "markdown" in exports and exports["markdown"].startswith("# Data Dictionary")


def test_versioning_compare_and_migration():
    old = make_schema()
    new = make_schema()
    new.tables[0].fields.append(SchemaField(name="name", type=FieldType.VARCHAR, length=120))
    new.tables.append(Table(name="payments", fields=[
        SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False)]))

    diff = compare_schemas(old, new)
    assert "payments" in diff["added_tables"]
    migration = diff_to_migration(old, new, diff)
    assert "CREATE TABLE payments" in migration
    assert "ALTER TABLE users ADD COLUMN name VARCHAR(120)" in migration


def test_versioning_drop_table_and_empty():
    old = make_schema()
    new = make_schema()
    new.tables = [t for t in new.tables if t.name != "orders"]
    migration = diff_to_migration(old, new)
    assert "DROP TABLE IF EXISTS orders;" in migration
    # first-version diff vs empty baseline = everything created
    first = diff_to_migration(empty_schema(), make_schema())
    assert "CREATE TABLE users" in first and "CREATE TABLE orders" in first


def test_http_compare_endpoint():
    client = TestClient(app)
    old = make_schema().model_dump(mode="json")
    new = make_schema()
    new.tables.append(Table(name="payments", fields=[
        SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False)]))
    resp = client.post("/compare", json={"old": old, "new": new.model_dump(mode="json")})
    assert resp.status_code == 200
    body = resp.json()
    assert "payments" in body["diff"]["added_tables"]
    assert "CREATE TABLE payments" in body["migration"]


def test_http_export_markdown():
    client = TestClient(app)
    schema = make_schema().model_dump(mode="json")
    resp = client.post("/export", json={"schema": schema, "type": "markdown"})
    assert resp.status_code == 200
    assert resp.json()["content"].startswith("# Data Dictionary")


def test_markdown_in_supported_frameworks():
    assert "markdown" in supported_frameworks()["export"]
