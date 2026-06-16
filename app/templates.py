"""Deterministic, domain-aware schema templates (the offline fallback for `suggest_schema`).

When no LLM is available we still want a useful starting schema. `build_template_schema` classifies
the request into a domain (ecommerce, blog, saas, generic) and returns a sensible normalized schema.
This keeps the module fully functional offline and makes tests deterministic.
"""

from __future__ import annotations

import re

from app.schema_model import DatabaseSchema, FieldType, Relation, RelationType, SchemaField, Table

_STOPWORDS = frozenset(
    {"build", "a", "an", "the", "for", "with", "and", "of", "to", "app", "application", "system",
     "platform", "service", "site", "website", "database", "db", "manage", "management", "simple",
     "create", "make", "i", "need", "want", "my"}
)

_ECOMMERCE_KW = ("store", "shop", "commerce", "ecommerce", "e-commerce", "retail", "cart", "checkout",
                 "product", "clothing", "marketplace", "order", "inventory", "catalog")
_BLOG_KW = ("blog", "cms", "article", "post", "news", "publish", "content", "magazine")
_SAAS_KW = ("saas", "subscription", "billing", "tenant", "workspace", "team", "dashboard")


def _id() -> SchemaField:
    return SchemaField(name="id", type=FieldType.BIGINT, primary_key=True, auto_increment=True, nullable=False)


def _fk(name: str) -> SchemaField:
    return SchemaField(name=name, type=FieldType.FOREIGN_ID, nullable=False, indexed=True)


def _varchar(name: str, *, length: int = 255, unique: bool = False, nullable: bool = True) -> SchemaField:
    return SchemaField(name=name, type=FieldType.VARCHAR, length=length, unique=unique, nullable=nullable)


def _decimal(name: str) -> SchemaField:
    return SchemaField(name=name, type=FieldType.DECIMAL, precision=10, scale=2, nullable=False)


def _ts(name: str) -> SchemaField:
    return SchemaField(name=name, type=FieldType.TIMESTAMP, nullable=False)


def _rel(from_table: str, from_field: str, to_table: str, rtype: RelationType = RelationType.MANY_TO_ONE) -> Relation:
    return Relation(name=f"{from_table}_{from_field}", from_table=from_table, from_field=from_field,
                    to_table=to_table, to_field="id", type=rtype)


def _users_table() -> Table:
    return Table(
        name="users",
        fields=[
            _id(),
            _varchar("name", nullable=False),
            _varchar("email", unique=True, nullable=False),
            _varchar("password", nullable=False),
            _ts("created_at"),
            _ts("updated_at"),
        ],
        description="Application users / accounts.",
    )


def _ecommerce_schema(driver: str) -> DatabaseSchema:
    users = _users_table()
    categories = Table(
        name="categories",
        fields=[_id(), _varchar("name", nullable=False), _varchar("slug", unique=True, nullable=False),
                _ts("created_at")],
        description="Product categories.",
    )
    products = Table(
        name="products",
        fields=[
            _id(), _fk("category_id"),
            _varchar("name", nullable=False), _varchar("slug", unique=True, nullable=False),
            SchemaField(name="description", type=FieldType.TEXT),
            _decimal("price"),
            SchemaField(name="stock", type=FieldType.INTEGER, nullable=False, default=0),
            SchemaField(name="is_active", type=FieldType.BOOLEAN, nullable=False, default=True),
            _ts("created_at"), _ts("updated_at"),
        ],
        relations=[_rel("products", "category_id", "categories")],
        description="Catalog products.",
    )
    orders = Table(
        name="orders",
        fields=[
            _id(), _fk("user_id"),
            SchemaField(name="status", type=FieldType.ENUM, nullable=False, default="pending",
                        values=["pending", "paid", "shipped", "delivered", "cancelled"]),
            _decimal("total"),
            _ts("created_at"), _ts("updated_at"),
        ],
        relations=[_rel("orders", "user_id", "users")],
        description="Customer orders.",
    )
    order_items = Table(
        name="order_items",
        fields=[
            _id(), _fk("order_id"), _fk("product_id"),
            SchemaField(name="quantity", type=FieldType.INTEGER, nullable=False, default=1),
            _decimal("unit_price"),
        ],
        relations=[_rel("order_items", "order_id", "orders"), _rel("order_items", "product_id", "products")],
        description="Line items for an order.",
    )
    payments = Table(
        name="payments",
        fields=[
            _id(), _fk("order_id"),
            _varchar("provider", nullable=False),
            _varchar("transaction_id", unique=True),
            _decimal("amount"),
            SchemaField(name="status", type=FieldType.ENUM, nullable=False, default="pending",
                        values=["pending", "succeeded", "failed", "refunded"]),
            _ts("created_at"),
        ],
        relations=[_rel("payments", "order_id", "orders")],
        description="Payment transactions.",
    )
    return DatabaseSchema(id="template-ecommerce", driver=driver,
                          tables=[users, categories, products, orders, order_items, payments],
                          metadata={"source": "template", "domain": "ecommerce"})


def _blog_schema(driver: str) -> DatabaseSchema:
    users = _users_table()
    posts = Table(
        name="posts",
        fields=[
            _id(), _fk("user_id"),
            _varchar("title", nullable=False), _varchar("slug", unique=True, nullable=False),
            SchemaField(name="body", type=FieldType.TEXT, nullable=False),
            SchemaField(name="published", type=FieldType.BOOLEAN, nullable=False, default=False),
            SchemaField(name="published_at", type=FieldType.TIMESTAMP),
            _ts("created_at"), _ts("updated_at"),
        ],
        relations=[_rel("posts", "user_id", "users")],
        description="Blog posts / articles.",
    )
    comments = Table(
        name="comments",
        fields=[
            _id(), _fk("post_id"), _fk("user_id"),
            SchemaField(name="body", type=FieldType.TEXT, nullable=False),
            _ts("created_at"),
        ],
        relations=[_rel("comments", "post_id", "posts"), _rel("comments", "user_id", "users")],
        description="Comments on posts.",
    )
    tags = Table(
        name="tags",
        fields=[_id(), _varchar("name", unique=True, nullable=False), _varchar("slug", unique=True, nullable=False)],
        description="Post tags.",
    )
    return DatabaseSchema(id="template-blog", driver=driver, tables=[users, posts, comments, tags],
                          metadata={"source": "template", "domain": "blog"})


def _saas_schema(driver: str) -> DatabaseSchema:
    users = _users_table()
    teams = Table(
        name="teams",
        fields=[_id(), _varchar("name", nullable=False), _varchar("slug", unique=True, nullable=False),
                _ts("created_at")],
        description="Workspaces / tenants.",
    )
    memberships = Table(
        name="memberships",
        fields=[
            _id(), _fk("team_id"), _fk("user_id"),
            SchemaField(name="role", type=FieldType.ENUM, nullable=False, default="member",
                        values=["owner", "admin", "member"]),
            _ts("created_at"),
        ],
        relations=[_rel("memberships", "team_id", "teams"), _rel("memberships", "user_id", "users")],
        description="User membership in a team.",
    )
    subscriptions = Table(
        name="subscriptions",
        fields=[
            _id(), _fk("team_id"),
            _varchar("plan", nullable=False),
            SchemaField(name="status", type=FieldType.ENUM, nullable=False, default="active",
                        values=["trialing", "active", "past_due", "cancelled"]),
            SchemaField(name="renews_at", type=FieldType.TIMESTAMP),
            _ts("created_at"),
        ],
        relations=[_rel("subscriptions", "team_id", "teams")],
        description="Billing subscriptions.",
    )
    return DatabaseSchema(id="template-saas", driver=driver, tables=[users, teams, memberships, subscriptions],
                          metadata={"source": "template", "domain": "saas"})


def _generic_schema(request: str, driver: str) -> DatabaseSchema:
    resource = _guess_resource(request)
    plural = _pluralize(resource)
    users = _users_table()
    resource_table = Table(
        name=plural,
        fields=[
            _id(), _fk("user_id"),
            _varchar("name", nullable=False),
            SchemaField(name="description", type=FieldType.TEXT),
            SchemaField(name="status", type=FieldType.VARCHAR, length=50, default="active"),
            _ts("created_at"), _ts("updated_at"),
        ],
        relations=[_rel(plural, "user_id", "users")],
        description=f"Primary '{resource}' records.",
    )
    return DatabaseSchema(id="template-generic", driver=driver, tables=[users, resource_table],
                          metadata={"source": "template", "domain": "generic"})


def build_template_schema(request: str, *, driver: str = "postgresql") -> DatabaseSchema:
    low = (request or "").lower()
    if any(k in low for k in _ECOMMERCE_KW):
        return _ecommerce_schema(driver)
    if any(k in low for k in _BLOG_KW):
        return _blog_schema(driver)
    if any(k in low for k in _SAAS_KW):
        return _saas_schema(driver)
    return _generic_schema(request, driver)


def _guess_resource(request: str) -> str:
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*", request or ""):
        if word.lower() not in _STOPWORDS and len(word) > 2:
            return word.lower()
    return "item"


def _pluralize(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"
