"""AI Suggest pipeline — the one and only LLM touchpoint of the greenfield path (spec §9).

Turning a free-text PRD into entities is genuinely not deterministic, so this is where the LLM is
allowed to help. Everything *after* the suggestion is deterministic and reproducible:

    PRD ──(LLM or heuristic)──▶ candidate schema_json
        ──▶ deterministic pipeline: assign Stable IDs · resolve semantic types · ensure a PK ·
            normalise relations · structural validate
        ──▶ suggestion (NEVER auto-applied — a human applies it via /apply-suggestion, AD-5)

Two hard guarantees from the spec:

* The path **works with no LLM at all** — a deterministic, domain-aware heuristic produces a valid,
  exportable schema from keywords, so the rest of the milestone never depends on a model.
* The output is a *suggestion*, returned alongside a ``diffFromCurrent`` (empty → suggestion) for the
  human to review; it is not written into the draft until they say so.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from app.core import diff as core_diff
from app.core import schema_json as core_sj
from app.core.ids import id_prefix, is_valid_id
from app.core.schema_json import CURRENT_FORMAT_VERSION
from app.core.type_system import DEFAULT_REGISTRY, infer_semantic_type

if TYPE_CHECKING:
    from aiarch_module_sdk import LLMClient


# --------------------------------------------------------------------------------------------------
# Deterministic stable-id minting (for normalising AI output / heuristic schemas).
# --------------------------------------------------------------------------------------------------
def _stable_id(prefix: str, *parts: str) -> str:
    """A deterministic, schema-valid id derived from the entity's identity (so it is reproducible)."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()  # 12 hex chars
    return f"{prefix}_{digest}"


def _resolve_type(semantic_type: str | None, field_name: str) -> str:
    if semantic_type and DEFAULT_REGISTRY.has(semantic_type):
        return semantic_type
    # Unknown/absent → reverse-infer a sensible default from the column name (deterministic).
    return infer_semantic_type("varchar", field_name).semantic_type


# --------------------------------------------------------------------------------------------------
# The deterministic normalisation pipeline (applied to BOTH LLM and heuristic candidates).
# --------------------------------------------------------------------------------------------------
def _normalize(candidate: dict[str, Any]) -> dict[str, Any]:
    data = core_sj.migrate(dict(candidate or {}))
    data.setdefault("formatVersion", CURRENT_FORMAT_VERSION)
    logical = data.setdefault("logical", {})
    tables = logical.setdefault("tables", [])

    name_to_id: dict[str, str] = {}
    for table in tables:
        tname = table.get("name") or "table"
        if not (is_valid_id(table.get("id")) and id_prefix(table.get("id")) == "tbl"):
            table["id"] = _stable_id("tbl", tname)
        name_to_id[tname] = table["id"]

        fields = table.setdefault("fields", [])
        if not any(f.get("isPrimaryKey") for f in fields):
            fields.insert(0, {"name": "id", "semanticType": "uuid", "isPrimaryKey": True, "nullable": False})
        for field in fields:
            fname = field.get("name") or "field"
            if not (is_valid_id(field.get("id")) and id_prefix(field.get("id")) == "fld"):
                field["id"] = _stable_id("fld", tname, fname)
            field["semanticType"] = _resolve_type(field.get("semanticType"), fname)
            field.setdefault("nullable", not field.get("isPrimaryKey", False))

    valid_ids = set(name_to_id.values())
    id_to_table = {t["id"]: t for t in tables}
    id_to_name = {tid: name for name, tid in name_to_id.items()}
    claimed_fk: set[str] = set()
    norm_relations: list[dict[str, Any]] = []
    for rel in logical.get("relations", []):
        frm = rel.get("fromTableId") or rel.get("fromTable")
        to = rel.get("toTableId") or rel.get("toTable")
        frm = name_to_id.get(frm, frm)
        to = name_to_id.get(to, to)
        if frm not in valid_ids or to not in valid_ids:
            continue  # drop relations whose endpoints we cannot resolve (AI hallucination guard)
        rid = rel.get("id")
        if not (is_valid_id(rid) and id_prefix(rid) == "rel"):
            rid = _stable_id("rel", frm, to)
        entry: dict[str, Any] = {
            "id": rid,
            "name": rel.get("name", "belongsTo"),
            "type": rel.get("type", "one_to_many"),
            "fromTableId": frm,
            "toTableId": to,
            **({"onDelete": rel["onDelete"]} if rel.get("onDelete") else {}),
        }
        # Link the relation to its foreign-key field. A producer (heuristic or LLM) often omits this,
        # but downstream the emitter then can't find the FK column (it guesses `<table>_id`) and the
        # Type System can't resolve the FK's physical type — so we resolve it here, deterministically.
        fk_field_id = rel.get("foreignKeyFieldId") or rel.get("foreign_key_field_id")
        if not (fk_field_id and any(f.get("id") == fk_field_id for f in id_to_table[frm]["fields"])):
            fk_field_id = _resolve_fk_field(id_to_table[frm], id_to_name.get(to, ""), claimed_fk)
        if fk_field_id:
            entry["foreignKeyFieldId"] = fk_field_id
            claimed_fk.add(fk_field_id)
        norm_relations.append(entry)
    if norm_relations or "relations" in logical:
        logical["relations"] = norm_relations
    return data


def _resolve_fk_field(from_table: dict[str, Any], to_name: str, claimed: set[str]) -> str | None:
    """Find the foreign-key field in ``from_table`` that backs a relation to ``to_name``.

    Prefer a conventionally-named column (``<singular(to)>_id`` / ``<to>_id``); otherwise fall back to
    the first still-unclaimed foreign-key field. Deterministic (field order is preserved)."""
    fks = [f for f in from_table.get("fields", [])
           if f.get("semanticType") == "foreign_key" and f.get("id") not in claimed]
    if not fks:
        return None
    singular = to_name[:-1] if to_name.endswith("s") else to_name
    for candidate in (f"{singular}_id", f"{to_name}_id"):
        for f in fks:
            if f.get("name") == candidate:
                return f.get("id")
    return fks[0].get("id")


# --------------------------------------------------------------------------------------------------
# Heuristic (no-LLM) PRD → schema. Domain-aware, keyword-driven, fully deterministic.
# --------------------------------------------------------------------------------------------------
def _field(name: str, semantic_type: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "semanticType": semantic_type, **kw}


def _pk() -> dict[str, Any]:
    return _field("id", "uuid", isPrimaryKey=True, nullable=False)


# Education / e-learning domain: any one of these keywords means "this is a courses platform", so the
# whole bundle (courses + students + enrollments + lessons + instructors) is seeded together — a single
# `courses` table for an "online course platform" PRD was the bug §1 symptom.
_KW_EDU = (
    "course", "courses", "education", "educational", "e-learning", "elearning", "learning", "lms",
    "online class", "online classes", "دوره", "آموزش", "آموزشی", "یادگیری", "کلاس آنلاین",
)


# Each entity: trigger keywords → (table name, fields, [relation target table names]).
_ENTITY_TEMPLATES: list[tuple[tuple[str, ...], str, list[dict[str, Any]], list[str]]] = [
    (("user", "users", "account", "کاربر", "حساب", "مشتری", "customer"), "users",
     [_pk(), _field("email", "email", nullable=False), _field("full_name", "string"),
      _field("created_at", "timestamp")], []),
    (("product", "products", "item", "محصول", "کالا"), "products",
     [_pk(), _field("name", "string", nullable=False), _field("price", "money", nullable=False),
      _field("description", "text")], []),
    (("category", "categories", "دسته"), "categories",
     [_pk(), _field("name", "string", nullable=False), _field("slug", "slug")], []),
    (("order", "orders", "سفارش"), "orders",
     [_pk(), _field("user_id", "foreign_key", nullable=False), _field("total", "money", nullable=False),
      _field("status", "status", nullable=False)], ["users"]),
    (("payment", "payments", "پرداخت", "transaction", "تراکنش"), "payments",
     [_pk(), _field("order_id", "foreign_key", nullable=False), _field("amount", "money", nullable=False),
      _field("status", "status", nullable=False)], ["orders"]),
    (("post", "posts", "article", "blog", "مقاله", "پست"), "posts",
     [_pk(), _field("title", "string", nullable=False), _field("body", "markdown"),
      _field("author_id", "foreign_key")], ["users"]),
    (("comment", "comments", "نظر", "دیدگاه"), "comments",
     [_pk(), _field("body", "text", nullable=False), _field("author_id", "foreign_key")], ["users"]),
    # --- Education / e-learning bundle (all share _KW_EDU so a single "course" PRD seeds the domain) ---
    (_KW_EDU + ("instructor", "instructors", "teacher", "teachers", "مدرس", "استاد", "معلم"), "instructors",
     [_pk(), _field("email", "email", nullable=False), _field("full_name", "string", nullable=False),
      _field("bio", "text")], []),
    (_KW_EDU, "courses",
     [_pk(), _field("title", "string", nullable=False), _field("description", "text"),
      _field("price", "money"), _field("instructor_id", "foreign_key")], ["instructors"]),
    (_KW_EDU + ("student", "students", "learner", "learners", "دانشجو", "دانش‌آموز", "فراگیر"), "students",
     [_pk(), _field("email", "email", nullable=False), _field("full_name", "string"),
      _field("enrolled_at", "timestamp")], []),
    (_KW_EDU + ("lesson", "lessons", "module", "modules", "درس", "جلسه", "محتوا"), "lessons",
     [_pk(), _field("course_id", "foreign_key", nullable=False), _field("title", "string", nullable=False),
      _field("body", "markdown"), _field("position", "integer")], ["courses"]),
    (_KW_EDU + ("enroll", "enrollment", "enrollments", "registration", "ثبت‌نام", "ثبت نام"), "enrollments",
     [_pk(), _field("student_id", "foreign_key", nullable=False),
      _field("course_id", "foreign_key", nullable=False), _field("status", "status", nullable=False),
      _field("enrolled_at", "timestamp")], ["students", "courses"]),
]


def _heuristic(prd: str) -> dict[str, Any]:
    text = (prd or "").lower()
    chosen: list[tuple[str, list[dict[str, Any]], list[str]]] = []
    present: set[str] = set()
    for keywords, table_name, fields, rel_targets in _ENTITY_TEMPLATES:
        if any(k in text for k in keywords):
            chosen.append((table_name, fields, rel_targets))
            present.add(table_name)

    if not chosen:  # nothing recognised → a single generic table keeps the pipeline meaningful
        chosen.append(("items", [_pk(), _field("name", "string", nullable=False), _field("description", "text")], []))
        present.add("items")

    tables = [{"name": name, "kind": "normal", "fields": fields} for name, fields, _ in chosen]
    relations: list[dict[str, Any]] = []
    for name, _fields, rel_targets in chosen:
        for target in rel_targets:
            if target in present:
                relations.append({"type": "one_to_many", "name": "belongsTo",
                                  "fromTable": name, "toTable": target, "onDelete": "cascade"})
    return {
        "formatVersion": CURRENT_FORMAT_VERSION,
        "meta": {"name": "suggested", "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {"tables": tables, "relations": relations},
    }


# --------------------------------------------------------------------------------------------------
# LLM candidate (best-effort; any failure falls back to the heuristic).
# --------------------------------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a senior database architect. Given a product description, output a single JSON object "
    "in the layered schema_json format: {\"formatVersion\":\"1.0.0\",\"logical\":{\"tables\":[...],"
    "\"relations\":[...]}}. Each table has a name and a fields array; each field has name and "
    "semanticType (one of: string,text,email,url,slug,password,uuid,integer,big_integer,decimal,"
    "money,boolean,date,datetime,timestamp,enum,status,json,foreign_key,image,file). Mark primary "
    "keys with isPrimaryKey:true. Use foreign_key for references and add relations with fromTable "
    "and toTable referencing table names. Output JSON only, no prose."
)


async def _llm_candidate(prd: str, llm: LLMClient) -> dict[str, Any] | None:
    try:
        result = await llm.complete(_SYSTEM_PROMPT, prd or "")
        return result if isinstance(result, dict) and result.get("logical") else None
    except Exception:  # noqa: BLE001 - any LLM failure must degrade to the heuristic, never raise
        return None


def _rationale(schema: core_sj.SchemaJson, source: str) -> str:
    tables = [t.name for t in schema.logical.tables]
    rels = len(schema.logical.relations)
    origin = "an LLM proposal" if source == "llm" else "the deterministic heuristic (no LLM configured)"
    return (
        f"Suggested {len(tables)} table(s) ({', '.join(tables)}) and {rels} relation(s) from {origin}. "
        "Stable IDs were assigned and semantic types resolved deterministically. This is a suggestion "
        "only — review the diff and apply it explicitly to the draft."
    )


# --------------------------------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------------------------------
async def suggest_schema(
    prd: str, current: dict[str, Any] | None = None, *, llm: LLMClient | None = None
) -> dict[str, Any]:
    """Produce a schema suggestion for ``prd``. Never applied; returns it for human review (AD-5)."""
    source = "heuristic"
    candidate: dict[str, Any] | None = None
    if llm is not None:
        candidate = await _llm_candidate(prd, llm)
        if candidate is not None:
            source = "llm"
    if candidate is None:
        candidate = _heuristic(prd)

    schema_dict = _normalize(candidate)
    if core_sj.validate_structure(schema_dict):
        # The candidate (likely from the LLM) normalised to something structurally invalid — fall
        # back to the guaranteed-valid heuristic so the pipeline always yields a usable suggestion.
        schema_dict = _normalize(_heuristic(prd))
        source = "heuristic"

    current_schema = core_sj.load(current, validate=False) if current else core_sj.SchemaJson()
    suggestion_schema = core_sj.load(schema_dict, validate=False)
    diff_ops = core_diff.diff(current_schema, suggestion_schema).op_dicts()
    return {
        "suggestion": schema_dict,
        "diffFromCurrent": diff_ops,
        "rationale": _rationale(suggestion_schema, source),
        "source": source,
    }
