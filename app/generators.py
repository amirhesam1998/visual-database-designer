"""Multi-framework code generators (Phase 6F enhancements — features #4, #21, #22).

Three families, all deterministic and LLM-free so the canvas can call them synchronously:

  * **Framework schema exporters** (#4) — a whole `DatabaseSchema` as a framework's schema/migration
    definition: Django models, SQLAlchemy models, TypeORM entities, Sequelize models. These extend the
    canonical five in `exporters.py` (sql/migration/prisma/mermaid/openapi) for the `/export` route.

  * **Model generators** (#21) — one `Table` as an ORM model/entity class, with relationships, casts
    and timestamps: Laravel Eloquent, TypeORM, SQLAlchemy, Django, Prisma.

  * **CRUD generators** (#22) — one `Table` as a full CRUD controller (index/show/store/update/destroy)
    with validation, error handling and proper status codes: Laravel, Express (TypeScript), Django REST.

Everything keys off the same `DatabaseSchema`/`Table`/`SchemaField` model, so a model and its CRUD
controller stay consistent. Relationships are derived from each table's `relations` + `*_id` fields.
"""

from __future__ import annotations

from app.schema_model import DatabaseSchema, FieldType, RelationType, SchemaField, Table

# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

_PLURAL_RULES = (("ies", "y"), ("ses", "s"), ("xes", "x"), ("zes", "z"), ("ches", "ch"), ("shes", "sh"))


def _pascal(snake: str) -> str:
    return "".join(word[:1].upper() + word[1:] for word in (snake or "").split("_") if word)


def _camel(snake: str) -> str:
    pascal = _pascal(snake)
    return pascal[:1].lower() + pascal[1:] if pascal else ""


def _singular(word: str) -> str:
    """Best-effort singularize a (snake_case) table name for a model/class name."""
    w = (word or "").strip()
    if not w:
        return w
    lower = w.lower()
    for suffix, repl in _PLURAL_RULES:
        if lower.endswith(suffix):
            return w[: -len(suffix)] + repl
    if lower.endswith("s") and not lower.endswith("ss"):
        return w[:-1]
    return w


def _model_name(table: Table) -> str:
    """PascalCase singular class name for a table (users → User)."""
    return _pascal(_singular(table.name))


def _is_fk(field: SchemaField) -> bool:
    return field.type == FieldType.FOREIGN_ID or field.name.endswith("_id")


def _is_timestamp_field(name: str) -> bool:
    return name in ("created_at", "updated_at", "deleted_at")


def _belongs_to_targets(schema: DatabaseSchema, table: Table) -> list[tuple[str, str]]:
    """Return (related_table, fk_field) pairs this table belongs to (its outgoing FK relations)."""
    out: list[tuple[str, str]] = []
    for rel in table.relations:
        if rel.type in (RelationType.MANY_TO_ONE, RelationType.ONE_TO_ONE):
            out.append((rel.to_table, rel.from_field or f"{_singular(rel.to_table)}_id"))
    # Fall back to *_id convention when no relations are declared.
    if not out:
        for f in table.fields:
            if _is_fk(f) and f.name != "id":
                out.append((f.name[:-3] + "s", f.name))
    return out


def _has_many_targets(schema: DatabaseSchema, table: Table) -> list[str]:
    """Return related tables that have a one-to-many/one-to-one pointing back at this table."""
    out: list[str] = []
    for other in schema.tables:
        if other.name == table.name:
            continue
        for rel in other.relations:
            if rel.to_table == table.name and rel.type in (
                RelationType.MANY_TO_ONE,
                RelationType.ONE_TO_MANY,
                RelationType.ONE_TO_ONE,
            ):
                out.append(other.name)
    return out


# ===========================================================================
# #21 — Model / entity generators
# ===========================================================================


class LaravelModelGenerator:
    """Eloquent model with $fillable, $hidden, $casts and relationship methods."""

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table) -> str:
        name = _model_name(table)
        fillable = [
            f.name for f in table.fields
            if not f.primary_key and not _is_timestamp_field(f.name)
        ]
        hidden = [f.name for f in table.fields if f.name in ("password", "remember_token")]
        casts = self._casts(table)

        lines = [
            "<?php",
            "",
            "namespace App\\Models;",
            "",
            "use Illuminate\\Database\\Eloquent\\Factories\\HasFactory;",
            "use Illuminate\\Database\\Eloquent\\Model;",
        ]
        if table.soft_delete:
            lines.append("use Illuminate\\Database\\Eloquent\\SoftDeletes;")
        lines += ["", f"class {name} extends Model", "{"]
        traits = ["HasFactory"] + (["SoftDeletes"] if table.soft_delete else [])
        lines.append(f"    use {', '.join(traits)};")
        lines.append("")
        lines.append(f"    protected $table = '{table.name}';")
        lines.append("")
        lines.append("    protected $fillable = [")
        lines += [f"        '{f}'," for f in fillable]
        lines.append("    ];")
        if hidden:
            lines += ["", "    protected $hidden = ["]
            lines += [f"        '{f}'," for f in hidden]
            lines.append("    ];")
        if casts:
            lines += ["", "    protected $casts = ["]
            lines += [f"        '{n}' => '{c}'," for n, c in casts]
            lines.append("    ];")
        for related, _ in _belongs_to_targets(self.schema, table):
            rel_model = _pascal(_singular(related))
            method = _camel(_singular(related))
            lines += [
                "",
                f"    public function {method}()",
                "    {",
                f"        return $this->belongsTo({rel_model}::class);",
                "    }",
            ]
        for related in _has_many_targets(self.schema, table):
            rel_model = _pascal(_singular(related))
            method = related  # plural relation name
            lines += [
                "",
                f"    public function {method}()",
                "    {",
                f"        return $this->hasMany({rel_model}::class);",
                "    }",
            ]
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _casts(self, table: Table) -> list[tuple[str, str]]:
        cast_map = {
            FieldType.BOOLEAN: "boolean",
            FieldType.JSON: "array",
            FieldType.DATE: "date",
            FieldType.DATETIME: "datetime",
            FieldType.TIMESTAMP: "datetime",
            FieldType.DECIMAL: "decimal:2",
        }
        out: list[tuple[str, str]] = []
        for f in table.fields:
            if f.primary_key or _is_timestamp_field(f.name):
                continue
            cast = cast_map.get(f.type)
            if cast:
                out.append((f.name, cast))
        return out


class TypeORMModelGenerator:
    """TypeORM entity with decorators, typed columns and relations."""

    _TS_TYPE = {
        FieldType.BIGINT: "number", FieldType.INTEGER: "number", FieldType.FOREIGN_ID: "number",
        FieldType.DECIMAL: "number", FieldType.NUMBER: "number",
        FieldType.VARCHAR: "string", FieldType.TEXT: "string", FieldType.STRING: "string",
        FieldType.UUID: "string", FieldType.ENUM: "string",
        FieldType.BOOLEAN: "boolean",
        FieldType.DATE: "Date", FieldType.DATETIME: "Date", FieldType.TIMESTAMP: "Date",
        FieldType.JSON: "object", FieldType.OBJECT: "object", FieldType.ARRAY: "any[]",
    }
    _COL_TYPE = {
        FieldType.BIGINT: "bigint", FieldType.INTEGER: "int", FieldType.FOREIGN_ID: "bigint",
        FieldType.DECIMAL: "decimal", FieldType.VARCHAR: "varchar", FieldType.TEXT: "text",
        FieldType.BOOLEAN: "boolean", FieldType.DATE: "date", FieldType.DATETIME: "timestamp",
        FieldType.TIMESTAMP: "timestamp", FieldType.JSON: "json", FieldType.UUID: "uuid",
        FieldType.ENUM: "varchar",
    }

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table) -> str:
        name = _model_name(table)
        imports = {"Entity", "Column", "PrimaryGeneratedColumn"}
        body: list[str] = []
        for f in table.fields:
            if _is_timestamp_field(f.name):
                continue
            if f.primary_key:
                imports.add("PrimaryGeneratedColumn")
                body += ["  @PrimaryGeneratedColumn()", f"  {f.name}: {self._ts(f)};", ""]
                continue
            body.append(self._column(f))
            body.append(f"  {f.name}: {self._ts(f)};")
            body.append("")
        if table.timestamps:
            imports.update({"CreateDateColumn", "UpdateDateColumn"})
            body += ["  @CreateDateColumn()", "  created_at: Date;", "",
                     "  @UpdateDateColumn()", "  updated_at: Date;", ""]
        related_imports: list[str] = []
        for related, _fk in _belongs_to_targets(self.schema, table):
            imports.add("ManyToOne")
            rel = _pascal(_singular(related))
            related_imports.append(rel)
            prop = _camel(_singular(related))
            body += [f"  @ManyToOne(() => {rel}, ({prop}) => {prop}.{table.name})",
                     f"  {prop}: {rel};", ""]
        for related in _has_many_targets(self.schema, table):
            imports.add("OneToMany")
            rel = _pascal(_singular(related))
            related_imports.append(rel)
            prop = related
            body += [f"  @OneToMany(() => {rel}, ({_camel(_singular(related))}) => "
                     f"{_camel(_singular(related))}.{_camel(_singular(table.name))})",
                     f"  {prop}: {rel}[];", ""]

        head = [f"import {{ {', '.join(sorted(imports))} }} from 'typeorm';"]
        for rel in sorted(set(related_imports)):
            if rel != name:
                head.append(f"import {{ {rel} }} from './{rel}';")
        head.append("")
        head.append(f"@Entity('{table.name}')")
        head.append(f"export class {name} {{")
        return "\n".join(head + body + ["}"]) + "\n"

    def _ts(self, f: SchemaField) -> str:
        return self._TS_TYPE.get(f.type, "string")

    def _column(self, f: SchemaField) -> str:
        opts: list[str] = [f"type: '{self._COL_TYPE.get(f.type, 'varchar')}'"]
        if f.type == FieldType.VARCHAR and f.length:
            opts.append(f"length: {f.length}")
        if f.unique:
            opts.append("unique: true")
        if f.nullable:
            opts.append("nullable: true")
        if f.default is not None:
            opts.append(f"default: {_js_value(f.default)}")
        return f"  @Column({{ {', '.join(opts)} }})"


class SQLAlchemyModelGenerator:
    """SQLAlchemy declarative model."""

    _SA_TYPE = {
        FieldType.BIGINT: "BigInteger", FieldType.INTEGER: "Integer", FieldType.FOREIGN_ID: "BigInteger",
        FieldType.VARCHAR: "String", FieldType.TEXT: "Text", FieldType.STRING: "String",
        FieldType.BOOLEAN: "Boolean", FieldType.DECIMAL: "Numeric",
        FieldType.DATE: "Date", FieldType.DATETIME: "DateTime", FieldType.TIMESTAMP: "DateTime",
        FieldType.JSON: "JSON", FieldType.UUID: "String", FieldType.ENUM: "String",
    }

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table) -> str:
        name = _model_name(table)
        used = {self._SA_TYPE.get(f.type, "String") for f in table.fields}
        if table.timestamps:
            used.add("DateTime")
        has_fk = any(_is_fk(f) and not f.primary_key for f in table.fields)
        if has_fk:
            used.add("ForeignKey")
        imports = sorted(used | {"Column"})
        lines = [
            f"from sqlalchemy import {', '.join(imports)}",
            "from sqlalchemy.orm import relationship",
            "from datetime import datetime",
            "",
            "from .base import Base",
            "",
            "",
            f"class {name}(Base):",
            f"    __tablename__ = '{table.name}'",
            "",
        ]
        for f in table.fields:
            if _is_timestamp_field(f.name):
                continue
            lines.append(f"    {f.name} = {self._column(f)}")
        if table.timestamps:
            lines.append("    created_at = Column(DateTime, default=datetime.utcnow)")
            lines.append("    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)")
        rels = []
        for related, _ in _belongs_to_targets(self.schema, table):
            rels.append(f"    {_camel(_singular(related))} = relationship('{_pascal(_singular(related))}', "
                        f"back_populates='{table.name}')")
        for related in _has_many_targets(self.schema, table):
            rels.append(f"    {related} = relationship('{_pascal(_singular(related))}', "
                        f"back_populates='{_camel(_singular(table.name))}')")
        if rels:
            lines.append("")
            lines += rels
        return "\n".join(lines) + "\n"

    def _column(self, f: SchemaField) -> str:
        sa_type = self._SA_TYPE.get(f.type, "String")
        type_expr = f"{sa_type}({f.length})" if f.type == FieldType.VARCHAR and f.length else sa_type
        args = [type_expr]
        if _is_fk(f) and not f.primary_key:
            args.append(f"ForeignKey('{f.name[:-3]}s.id')")
        if f.primary_key:
            args.append("primary_key=True")
        if f.unique and not f.primary_key:
            args.append("unique=True")
        if not f.nullable and not f.primary_key:
            args.append("nullable=False")
        if f.default is not None:
            args.append(f"default={_py_value(f.default)}")
        return f"Column({', '.join(args)})"


class DjangoModelGenerator:
    """Django ORM model."""

    _DJ_FIELD = {
        FieldType.BIGINT: "BigIntegerField", FieldType.INTEGER: "IntegerField",
        FieldType.VARCHAR: "CharField", FieldType.TEXT: "TextField", FieldType.STRING: "CharField",
        FieldType.BOOLEAN: "BooleanField", FieldType.DECIMAL: "DecimalField",
        FieldType.DATE: "DateField", FieldType.DATETIME: "DateTimeField", FieldType.TIMESTAMP: "DateTimeField",
        FieldType.JSON: "JSONField", FieldType.UUID: "UUIDField", FieldType.ENUM: "CharField",
    }

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table) -> str:
        name = _model_name(table)
        fk_fields = {fk for _, fk in _belongs_to_targets(self.schema, table)}
        lines = ["from django.db import models", "", "", f"class {name}(models.Model):"]
        for f in table.fields:
            if f.primary_key and f.auto_increment:
                continue  # Django adds an implicit auto id
            if _is_timestamp_field(f.name):
                continue
            if f.name in fk_fields:
                related = f.name[:-3]
                lines.append(
                    f"    {related} = models.ForeignKey('{_pascal(_singular(related + 's'))}', "
                    f"on_delete=models.CASCADE, related_name='{table.name}')"
                )
                continue
            lines.append(f"    {f.name} = {self._field(f)}")
        if table.timestamps:
            lines.append("    created_at = models.DateTimeField(auto_now_add=True)")
            lines.append("    updated_at = models.DateTimeField(auto_now=True)")
        lines += ["", "    class Meta:", f"        db_table = '{table.name}'"]
        return "\n".join(lines) + "\n"

    def _field(self, f: SchemaField) -> str:
        dj = self._DJ_FIELD.get(f.type, "CharField")
        args: list[str] = []
        if f.type == FieldType.VARCHAR:
            args.append(f"max_length={f.length or 255}")
        if f.type == FieldType.DECIMAL:
            args.append(f"max_digits={f.precision or 10}, decimal_places={f.scale or 2}")
        if f.unique and not f.primary_key:
            args.append("unique=True")
        if f.nullable:
            args.append("null=True, blank=True")
        if f.default is not None:
            args.append(f"default={_py_value(f.default)}")
        return f"models.{dj}({', '.join(args)})"


class PrismaModelGenerator:
    """A single Prisma model block (subset of the full-schema PrismaExporter)."""

    _PRISMA_TYPE = {
        FieldType.BIGINT: "BigInt", FieldType.INTEGER: "Int", FieldType.FOREIGN_ID: "BigInt",
        FieldType.VARCHAR: "String", FieldType.TEXT: "String", FieldType.STRING: "String",
        FieldType.BOOLEAN: "Boolean", FieldType.DECIMAL: "Decimal",
        FieldType.DATE: "DateTime", FieldType.DATETIME: "DateTime", FieldType.TIMESTAMP: "DateTime",
        FieldType.JSON: "Json", FieldType.UUID: "String", FieldType.ENUM: "String",
    }

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table) -> str:
        lines = [f"model {_model_name(table)} {{"]
        for f in table.fields:
            ptype = self._PRISMA_TYPE.get(f.type, "String")
            optional = "?" if f.nullable and not f.primary_key else ""
            attrs = []
            if f.primary_key:
                attrs.append("@id")
                if f.auto_increment:
                    attrs.append("@default(autoincrement())")
            if f.unique and not f.primary_key:
                attrs.append("@unique")
            suffix = (" " + " ".join(attrs)) if attrs else ""
            lines.append(f"  {f.name} {ptype}{optional}{suffix}")
        lines.append(f'  @@map("{table.name}")')
        lines.append("}")
        return "\n".join(lines) + "\n"


MODEL_GENERATORS = {
    "laravel": LaravelModelGenerator,
    "typeorm": TypeORMModelGenerator,
    "sqlalchemy": SQLAlchemyModelGenerator,
    "django": DjangoModelGenerator,
    "prisma": PrismaModelGenerator,
}


# ===========================================================================
# #22 — CRUD controller generators
# ===========================================================================

_CRUD_METHODS = ("index", "show", "store", "update", "destroy")


def _validation_field(f: SchemaField) -> list[str]:
    """Laravel validation rule tokens for a field."""
    rules: list[str] = []
    rules.append("required" if not f.nullable and f.default is None else "nullable")
    if f.name == "email" or f.type == FieldType.VARCHAR and "email" in f.name:
        rules.append("email")
    elif f.type in (FieldType.VARCHAR, FieldType.TEXT, FieldType.STRING):
        rules.append("string")
        if f.length:
            rules.append(f"max:{f.length}")
    elif f.type in (FieldType.INTEGER, FieldType.BIGINT, FieldType.FOREIGN_ID):
        rules.append("integer")
    elif f.type == FieldType.BOOLEAN:
        rules.append("boolean")
    elif f.type == FieldType.DECIMAL:
        rules.append("numeric")
    if f.name == "password":
        rules.append("min:8")
    return rules


class LaravelCrudGenerator:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table, methods: list[str]) -> str:
        model = _model_name(table)
        editable = [f for f in table.fields if not f.primary_key and not _is_timestamp_field(f.name)]
        has_password = any(f.name == "password" for f in editable)
        rules = ",\n".join(
            f"            '{f.name}' => '{'|'.join(_validation_field(f))}'" for f in editable
        )
        out = [
            "<?php",
            "",
            "namespace App\\Http\\Controllers;",
            "",
            f"use App\\Models\\{model};",
            "use Illuminate\\Http\\Request;",
            "",
            f"class {model}Controller extends Controller",
            "{",
        ]
        if "index" in methods:
            out += [
                "    // LIST: GET",
                "    public function index()",
                "    {",
                f"        return response()->json({model}::paginate(15), 200);",
                "    }",
                "",
            ]
        if "show" in methods:
            out += [
                "    // SHOW: GET /{id}",
                "    public function show($id)",
                "    {",
                f"        return response()->json({model}::findOrFail($id), 200);",
                "    }",
                "",
            ]
        if "store" in methods:
            out += [
                "    // CREATE: POST",
                "    public function store(Request $request)",
                "    {",
                "        $validated = $request->validate([",
                rules + ",",
                "        ]);",
            ]
            if has_password:
                out.append("        $validated['password'] = bcrypt($validated['password']);")
            out += [
                f"        $item = {model}::create($validated);",
                "        return response()->json($item, 201);",
                "    }",
                "",
            ]
        if "update" in methods:
            out += [
                "    // UPDATE: PUT /{id}",
                "    public function update(Request $request, $id)",
                "    {",
                f"        $item = {model}::findOrFail($id);",
                "        $validated = $request->validate([",
                rules + ",",
                "        ]);",
                "        $item->update($validated);",
                "        return response()->json($item, 200);",
                "    }",
                "",
            ]
        if "destroy" in methods:
            out += [
                "    // DELETE: DELETE /{id}",
                "    public function destroy($id)",
                "    {",
                f"        {model}::findOrFail($id)->delete();",
                "        return response()->json(null, 204);",
                "    }",
                "",
            ]
        out.append("}")
        return "\n".join(out) + "\n"


class ExpressCrudGenerator:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table, methods: list[str]) -> str:
        model = _model_name(table)
        editable = [f for f in table.fields if not f.primary_key and not _is_timestamp_field(f.name)]
        required = [f.name for f in editable if not f.nullable and f.default is None]
        has_password = any(f.name == "password" for f in editable)
        names = ", ".join(f.name for f in editable)
        out = [
            "import { Router, Request, Response } from 'express';",
            f"import {{ {model} }} from '../models/{model}';",
        ]
        if has_password:
            out.append("import bcrypt from 'bcrypt';")
        out += ["", "const router = Router();", ""]
        if "index" in methods:
            out += [
                "// LIST: GET /",
                "router.get('/', async (req: Request, res: Response) => {",
                "  try {",
                f"    const items = await {model}.find().limit(15);",
                "    res.json(items);",
                "  } catch (error: any) {",
                "    res.status(500).json({ error: error.message });",
                "  }",
                "});",
                "",
            ]
        if "show" in methods:
            out += [
                "// SHOW: GET /:id",
                "router.get('/:id', async (req: Request, res: Response) => {",
                "  try {",
                f"    const item = await {model}.findById(req.params.id);",
                "    if (!item) return res.status(404).json({ error: 'Not found' });",
                "    res.json(item);",
                "  } catch (error: any) {",
                "    res.status(500).json({ error: error.message });",
                "  }",
                "});",
                "",
            ]
        if "store" in methods:
            out += [
                "// CREATE: POST /",
                "router.post('/', async (req: Request, res: Response) => {",
                "  try {",
                f"    const {{ {names} }} = req.body;",
            ]
            if required:
                cond = " || ".join(f"!{r}" for r in required)
                out += [
                    f"    if ({cond}) {{",
                    "      return res.status(400).json({ error: 'Missing required fields' });",
                    "    }",
                ]
            if has_password:
                out.append("    const hashed = await bcrypt.hash(password, 10);")
                payload = ", ".join("password: hashed" if n == "password" else n for n in (f.name for f in editable))
                out.append(f"    const item = await {model}.create({{ {payload} }});")
            else:
                out.append(f"    const item = await {model}.create({{ {names} }});")
            out += [
                "    res.status(201).json(item);",
                "  } catch (error: any) {",
                "    res.status(500).json({ error: error.message });",
                "  }",
                "});",
                "",
            ]
        if "update" in methods:
            out += [
                "// UPDATE: PUT /:id",
                "router.put('/:id', async (req: Request, res: Response) => {",
                "  try {",
                f"    const item = await {model}.findById(req.params.id);",
                "    if (!item) return res.status(404).json({ error: 'Not found' });",
                "    await item.updateOne(req.body);",
                "    res.json(item);",
                "  } catch (error: any) {",
                "    res.status(500).json({ error: error.message });",
                "  }",
                "});",
                "",
            ]
        if "destroy" in methods:
            out += [
                "// DELETE: DELETE /:id",
                "router.delete('/:id', async (req: Request, res: Response) => {",
                "  try {",
                f"    const item = await {model}.findByIdAndDelete(req.params.id);",
                "    if (!item) return res.status(404).json({ error: 'Not found' });",
                "    res.status(204).send();",
                "  } catch (error: any) {",
                "    res.status(500).json({ error: error.message });",
                "  }",
                "});",
                "",
            ]
        out += ["export default router;"]
        return "\n".join(out) + "\n"


class DjangoCrudGenerator:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def generate(self, table: Table, methods: list[str]) -> str:
        model = _model_name(table)
        out = [
            "from rest_framework import viewsets",
            "from rest_framework.response import Response",
            "from rest_framework import status",
            "",
            f"from .models import {model}",
            f"from .serializers import {model}Serializer",
            "",
            "",
            f"class {model}ViewSet(viewsets.ModelViewSet):",
            f"    queryset = {model}.objects.all()",
            f"    serializer_class = {model}Serializer",
        ]
        if "store" in methods:
            out += [
                "",
                "    def create(self, request, *args, **kwargs):",
                "        serializer = self.get_serializer(data=request.data)",
                "        serializer.is_valid(raise_exception=True)",
                "        self.perform_create(serializer)",
                "        return Response(serializer.data, status=status.HTTP_201_CREATED)",
            ]
        if "update" in methods:
            out += [
                "",
                "    def update(self, request, *args, **kwargs):",
                "        instance = self.get_object()",
                "        serializer = self.get_serializer(instance, data=request.data, partial=True)",
                "        serializer.is_valid(raise_exception=True)",
                "        self.perform_update(serializer)",
                "        return Response(serializer.data)",
            ]
        if "destroy" in methods:
            out += [
                "",
                "    def destroy(self, request, *args, **kwargs):",
                "        instance = self.get_object()",
                "        self.perform_destroy(instance)",
                "        return Response(status=status.HTTP_204_NO_CONTENT)",
            ]
        return "\n".join(out) + "\n"


CRUD_GENERATORS = {
    "laravel": LaravelCrudGenerator,
    "express": ExpressCrudGenerator,
    "django": DjangoCrudGenerator,
}


# ===========================================================================
# #4 — Framework full-schema exporters (extend the canonical five)
# ===========================================================================


class DjangoSchemaExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        blocks = [DjangoModelGenerator(self.schema).generate(t) for t in self.schema.tables]
        return "from django.db import models\n\n\n" + "\n\n".join(
            b.split("\n\n", 1)[1] if b.startswith("from django") else b for b in blocks
        )


class SQLAlchemySchemaExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        return "\n\n".join(SQLAlchemyModelGenerator(self.schema).generate(t) for t in self.schema.tables)


class TypeORMSchemaExporter:
    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        return "\n\n".join(TypeORMModelGenerator(self.schema).generate(t) for t in self.schema.tables)


class SequelizeSchemaExporter:
    _SEQ_TYPE = {
        FieldType.BIGINT: "DataTypes.BIGINT", FieldType.INTEGER: "DataTypes.INTEGER",
        FieldType.FOREIGN_ID: "DataTypes.BIGINT", FieldType.VARCHAR: "DataTypes.STRING",
        FieldType.TEXT: "DataTypes.TEXT", FieldType.STRING: "DataTypes.STRING",
        FieldType.BOOLEAN: "DataTypes.BOOLEAN", FieldType.DECIMAL: "DataTypes.DECIMAL",
        FieldType.DATE: "DataTypes.DATEONLY", FieldType.DATETIME: "DataTypes.DATE",
        FieldType.TIMESTAMP: "DataTypes.DATE", FieldType.JSON: "DataTypes.JSON",
        FieldType.UUID: "DataTypes.UUID", FieldType.ENUM: "DataTypes.STRING",
    }

    def __init__(self, schema: DatabaseSchema):
        self.schema = schema

    def export(self) -> str:
        out = ["import { DataTypes, Model, Sequelize } from 'sequelize';", ""]
        for table in self.schema.tables:
            name = _model_name(table)
            out += [f"export class {name} extends Model {{}}", "", f"{name}.init({{"]
            for f in table.fields:
                if _is_timestamp_field(f.name):
                    continue
                attrs = [f"type: {self._SEQ_TYPE.get(f.type, 'DataTypes.STRING')}"]
                if f.primary_key:
                    attrs.append("primaryKey: true")
                if f.auto_increment:
                    attrs.append("autoIncrement: true")
                if f.unique and not f.primary_key:
                    attrs.append("unique: true")
                if not f.nullable and not f.primary_key:
                    attrs.append("allowNull: false")
                out.append(f"  {f.name}: {{ {', '.join(attrs)} }},")
            out += [
                "}, {",
                "  sequelize: new Sequelize(process.env.DATABASE_URL!),",
                f"  tableName: '{table.name}',",
                f"  timestamps: {str(table.timestamps).lower()},",
                "});",
                "",
            ]
        return "\n".join(out)


FRAMEWORK_SCHEMA_EXPORTERS = {
    "django": DjangoSchemaExporter,
    "sqlalchemy": SQLAlchemySchemaExporter,
    "typeorm": TypeORMSchemaExporter,
    "sequelize": SequelizeSchemaExporter,
}


# ---------------------------------------------------------------------------
# Value rendering helpers
# ---------------------------------------------------------------------------


def _js_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{value}'"


def _py_value(value) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{value}'"


# ---------------------------------------------------------------------------
# Façade
# ---------------------------------------------------------------------------


def _resolve_table(schema: DatabaseSchema, table_name: str | None) -> Table:
    if table_name:
        table = schema.table(table_name)
        if table is None:
            raise ValueError(f"unknown table: {table_name}")
        return table
    if not schema.tables:
        raise ValueError("schema has no tables")
    return schema.tables[0]


def generate_model(schema: DatabaseSchema, framework: str, table_name: str | None = None) -> str:
    gen_cls = MODEL_GENERATORS.get(framework)
    if gen_cls is None:
        raise ValueError(f"unsupported model framework: {framework}")
    return gen_cls(schema).generate(_resolve_table(schema, table_name))


def generate_crud(
    schema: DatabaseSchema, framework: str, table_name: str | None = None,
    methods: list[str] | None = None,
) -> str:
    gen_cls = CRUD_GENERATORS.get(framework)
    if gen_cls is None:
        raise ValueError(f"unsupported crud framework: {framework}")
    selected = [m for m in (methods or _CRUD_METHODS) if m in _CRUD_METHODS] or list(_CRUD_METHODS)
    return gen_cls(schema).generate(_resolve_table(schema, table_name), selected)


def export_framework_schema(schema: DatabaseSchema, framework: str) -> str:
    exp_cls = FRAMEWORK_SCHEMA_EXPORTERS.get(framework)
    if exp_cls is None:
        raise ValueError(f"unsupported schema framework: {framework}")
    return exp_cls(schema).export()


def supported_frameworks() -> dict[str, list[str]]:
    """What the UI dropdowns offer, per capability."""
    return {
        "export": ["sql", "migration", "prisma", "mermaid", "openapi", "markdown", *FRAMEWORK_SCHEMA_EXPORTERS],
        "model": list(MODEL_GENERATORS),
        "crud": list(CRUD_GENERATORS),
        "crud_methods": list(_CRUD_METHODS),
    }
