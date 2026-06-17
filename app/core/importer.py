"""Brownfield Importer — a real Postgres database → ``schema_json`` (Milestone 2 §1).

This is the mirror image of the SQL emitter: the emitter turns a design into DDL; the importer reads
a live database back into a layered, Stable-ID ``schema_json``. Two halves, deliberately separated so
the hard part is testable without a server (the lesson Milestone 1 taught — snapshots aren't proof):

* **Introspection** (:func:`introspect_postgres`) — the only impure part. It runs ``information_schema``
  / ``pg_catalog`` queries over a real connection and returns plain, JSON-able
  :class:`IntrospectedSchema` data. ``psycopg`` is imported lazily so the module loads without it.
* **Build** (:func:`build_schema_json`) — pure and deterministic. It turns introspected data into a
  ``schema_json``: Stable IDs (AD-1), deterministic reverse-inference of semantic types (AD-2/AD-5 —
  deterministic first, LLM only as an optional second pass), relations rebuilt from real foreign keys
  (with the FK column's type read straight from the database, so it is structurally correct by
  construction — the inverse of the bug M1 caught), indexes, Postgres enums and check constraints.

Two imports of the same database produce **byte-identical** ``schema_json`` (spec §4): every id is a
deterministic function of names, and everything is sorted.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field

from app.core import schema_json as core_sj
from app.core import validation as core_validation
from app.core.schema_json import CURRENT_FORMAT_VERSION
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, infer_semantic_type

# Confidence below which a reverse-inference is treated as "ambiguous" and surfaced for human
# confirmation (AD-5) rather than silently trusted.
_AMBIGUOUS_BELOW = 0.75
# Physical types that are genuinely ambiguous regardless of confidence (free-form blobs) — the spec
# calls these out as the LLM layer's job to enrich (§1.2).
_AMBIGUOUS_PHYSICAL = {"text", "json", "jsonb"}


# ==================================================================================================
# Introspected data model (plain, JSON-able — the boundary between impure I/O and the pure build).
# ==================================================================================================
class IntrospectedColumn(BaseModel):
    name: str
    data_type: str  # information_schema.data_type, e.g. "character varying", "integer", "USER-DEFINED"
    udt_name: str | None = None  # underlying type name (enum type name when data_type=USER-DEFINED)
    nullable: bool = True
    default: str | None = None
    char_max_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    is_identity: bool = False


class IntrospectedTable(BaseModel):
    name: str
    columns: list[IntrospectedColumn] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    checks: list[dict[str, str]] = Field(default_factory=list)  # [{name, clause}]


class IntrospectedForeignKey(BaseModel):
    name: str
    table: str
    columns: list[str] = Field(default_factory=list)
    ref_table: str
    ref_columns: list[str] = Field(default_factory=list)
    on_delete: str | None = None
    on_update: str | None = None


class IntrospectedIndex(BaseModel):
    name: str
    table: str
    columns: list[str] = Field(default_factory=list)
    unique: bool = False


class IntrospectedEnum(BaseModel):
    name: str
    labels: list[str] = Field(default_factory=list)


class IntrospectedSchema(BaseModel):
    tables: list[IntrospectedTable] = Field(default_factory=list)
    foreign_keys: list[IntrospectedForeignKey] = Field(default_factory=list)
    indexes: list[IntrospectedIndex] = Field(default_factory=list)
    enums: list[IntrospectedEnum] = Field(default_factory=list)


# ==================================================================================================
# Introspection (impure — the only place that touches a database).
# ==================================================================================================
_Q_TABLES = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = %s AND table_type = 'BASE TABLE'
ORDER BY table_name;
"""

_Q_COLUMNS = """
SELECT table_name, column_name, ordinal_position, data_type, udt_name,
       is_nullable, column_default, character_maximum_length,
       numeric_precision, numeric_scale, is_identity
FROM information_schema.columns
WHERE table_schema = %s
ORDER BY table_name, ordinal_position;
"""

_Q_PRIMARY_KEYS = """
SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
ORDER BY tc.table_name, kcu.ordinal_position;
"""

_Q_FOREIGN_KEYS = """
SELECT tc.constraint_name, tc.table_name, kcu.column_name, kcu.ordinal_position,
       ccu.table_name AS ref_table, ccu.column_name AS ref_column,
       rc.delete_rule, rc.update_rule
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
JOIN information_schema.constraint_column_usage ccu
  ON rc.unique_constraint_name = ccu.constraint_name AND rc.unique_constraint_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
ORDER BY tc.constraint_name, kcu.ordinal_position;
"""

_Q_INDEXES = """
SELECT t.relname AS table_name, i.relname AS index_name,
       ix.indisunique AS is_unique, ix.indisprimary AS is_primary,
       a.attname AS column_name, k.ord
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND t.relkind = 'r'
ORDER BY t.relname, i.relname, k.ord;
"""

_Q_ENUMS = """
SELECT t.typname AS enum_name, e.enumlabel AS label, e.enumsortorder
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = %s
ORDER BY t.typname, e.enumsortorder;
"""

_Q_CHECKS = """
SELECT tc.table_name, tc.constraint_name, cc.check_clause
FROM information_schema.table_constraints tc
JOIN information_schema.check_constraints cc
  ON tc.constraint_name = cc.constraint_name AND tc.table_schema = cc.constraint_schema
WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = %s
ORDER BY tc.table_name, tc.constraint_name;
"""


def _connect(dsn: str):  # noqa: ANN202 - returns a psycopg/psycopg2 connection
    """Lazily import a Postgres driver (psycopg 3 preferred, psycopg2 fallback) and connect."""
    try:
        import psycopg  # type: ignore

        return psycopg.connect(dsn)
    except ModuleNotFoundError:
        try:
            import psycopg2  # type: ignore

            return psycopg2.connect(dsn)
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "no Postgres driver installed; add psycopg[binary] (or psycopg2) to import a database"
            ) from exc


def introspect_postgres(dsn: str, *, schema: str = "public") -> IntrospectedSchema:
    """Read a live Postgres schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            def rows(sql: str) -> list[tuple]:
                cur.execute(sql, (schema,))
                return list(cur.fetchall())

            tables: dict[str, IntrospectedTable] = {
                r[0]: IntrospectedTable(name=r[0]) for r in rows(_Q_TABLES)
            }
            for (tname, cname, _ord, dtype, udt, is_null, default, clen, nprec, nscale, ident) in rows(_Q_COLUMNS):
                if tname not in tables:
                    continue
                tables[tname].columns.append(IntrospectedColumn(
                    name=cname, data_type=dtype, udt_name=udt,
                    nullable=(str(is_null).upper() == "YES"),
                    default=default, char_max_length=clen,
                    numeric_precision=nprec, numeric_scale=nscale,
                    is_identity=(str(ident).upper() == "YES"),
                ))
            for (tname, cname, _ord) in rows(_Q_PRIMARY_KEYS):
                if tname in tables:
                    tables[tname].primary_key.append(cname)
            for (tname, cstr, clause) in rows(_Q_CHECKS):
                # Skip the implicit NOT NULL checks Postgres reports here — they are column attributes.
                if tname in tables and "IS NOT NULL" not in (clause or "").upper():
                    tables[tname].checks.append({"name": cstr, "clause": clause})

            fks: dict[str, IntrospectedForeignKey] = {}
            for (cstr, tname, col, _ord, ref_table, ref_col, del_rule, upd_rule) in rows(_Q_FOREIGN_KEYS):
                fk = fks.get(cstr)
                if fk is None:
                    fk = IntrospectedForeignKey(name=cstr, table=tname, ref_table=ref_table,
                                                on_delete=_rule(del_rule), on_update=_rule(upd_rule))
                    fks[cstr] = fk
                fk.columns.append(col)
                fk.ref_columns.append(ref_col)

            idx: dict[tuple[str, str], IntrospectedIndex] = {}
            for (tname, iname, is_unique, is_primary, col, _ord) in rows(_Q_INDEXES):
                if is_primary:
                    continue  # the PK index is represented by the table's primary_key, not as an index
                key = (tname, iname)
                if key not in idx:
                    idx[key] = IntrospectedIndex(name=iname, table=tname, unique=bool(is_unique))
                idx[key].columns.append(col)

            enums: dict[str, IntrospectedEnum] = {}
            for (ename, label, _ord) in rows(_Q_ENUMS):
                enums.setdefault(ename, IntrospectedEnum(name=ename)).labels.append(label)

        return IntrospectedSchema(
            tables=sorted(tables.values(), key=lambda t: t.name),
            foreign_keys=sorted(fks.values(), key=lambda f: f.name),
            indexes=sorted(idx.values(), key=lambda i: (i.table, i.name)),
            enums=sorted(enums.values(), key=lambda e: e.name),
        )
    finally:
        conn.close()


def _rule(rule: str | None) -> str | None:
    """Normalise a referential action; drop the Postgres default (NO ACTION) to reduce noise."""
    if not rule:
        return None
    norm = rule.strip().lower().replace(" ", "_")
    return None if norm == "no_action" else norm


def split_sql(text: str) -> list[str]:
    """Split a raw-SQL migration file into individual statements (dollar-quote / quote aware).

    Good enough for the DDL migrations Milestone 2 targets (§2.2): it respects ``$$``/``$tag$``
    blocks and single-quoted strings so statement-terminating semicolons inside them are ignored.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    in_single = False
    dollar_tag: str | None = None
    while i < len(text):
        ch = text[i]
        if dollar_tag:
            if text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_single:
            if ch == "'":
                in_single = False
        elif ch == "'":
            in_single = True
        elif ch == "$":
            end = text.find("$", i + 1)
            if end != -1 and text[i + 1:end].isidentifier() or (end != -1 and i + 1 == end):
                dollar_tag = text[i:end + 1]
                buf.append(dollar_tag)
                i = end + 1
                continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_sql(dsn: str, statements: list[str]) -> None:
    """Apply raw-SQL statements in order to a (shadow) database — Leg B builder (impure; spec §2.2).

    ``autocommit`` is on so statements that cannot run in a transaction (e.g.
    ``CREATE INDEX CONCURRENTLY``) work, mirroring how the emitter's migrations are meant to run.
    """
    conn = _connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                s = stmt.strip()
                if s and not s.startswith("--"):
                    cur.execute(s)
    finally:
        conn.close()


# ==================================================================================================
# Stable-ID minting (deterministic — same name → same id, so two imports are byte-identical).
# ==================================================================================================
def _sid(prefix: str, *parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()  # 12 hex chars
    return f"{prefix}_{digest}"


# ==================================================================================================
# Physical-type mapping (introspected column → our physical spec dict).
# ==================================================================================================
def _physical_of(col: IntrospectedColumn) -> dict[str, Any]:
    """Map an introspected column to a physical spec dict (``{type, length?, precision?, scale?}``)."""
    dt = (col.data_type or "").strip().lower()
    udt = (col.udt_name or "").strip().lower()
    if dt in {"character varying", "varchar"}:
        return {"type": "varchar", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt in {"character", "char", "bpchar"}:
        return {"type": "char", **({"length": col.char_max_length} if col.char_max_length else {})}
    if dt == "text":
        return {"type": "text"}
    if dt in {"integer", "int", "int4"}:
        return {"type": "integer"}
    if dt in {"bigint", "int8"}:
        return {"type": "bigint"}
    if dt in {"smallint", "int2"}:
        return {"type": "smallint"}
    if dt in {"numeric", "decimal"}:
        spec: dict[str, Any] = {"type": "numeric"}
        if col.numeric_precision is not None:
            spec["precision"] = col.numeric_precision
            spec["scale"] = col.numeric_scale or 0
        return spec
    if dt in {"double precision", "real"}:
        return {"type": dt}
    if dt == "boolean":
        return {"type": "boolean"}
    if dt == "uuid":
        return {"type": "uuid"}
    if dt in {"timestamp without time zone", "timestamp with time zone", "timestamp"}:
        return {"type": "timestamp"}
    if dt == "date":
        return {"type": "date"}
    if dt in {"time without time zone", "time with time zone", "time"}:
        return {"type": "time"}
    if dt == "jsonb":
        return {"type": "jsonb"}
    if dt == "json":
        return {"type": "json"}
    if dt == "user-defined" and udt:
        return {"type": udt}  # an enum or other user type; recorded as-is
    # Unknown physical type — keep it verbatim (a safe, non-aggressive fallback; spec §1.2).
    return {"type": udt or dt or "text"}


def _is_autoincrement(col: IntrospectedColumn) -> bool:
    if col.is_identity:
        return True
    default = (col.default or "").lower()
    return "nextval(" in default


# ==================================================================================================
# Build (pure, deterministic): IntrospectedSchema → schema_json + inference + validation report.
# ==================================================================================================
def build_schema_json(
    introspected: IntrospectedSchema, *, name: str = "imported", reg: TypeRegistry | None = None
) -> dict[str, Any]:
    """Turn introspected data into ``{schema_json, inference, validation}`` (pure, deterministic)."""
    registry = reg or DEFAULT_REGISTRY

    enum_name_to_id: dict[str, str] = {}
    enums_out: list[dict[str, Any]] = []
    for en in introspected.enums:
        eid = _sid("enm", en.name)
        enum_name_to_id[en.name] = eid
        enums_out.append({
            "id": eid, "name": en.name,
            "values": [{"value": label} for label in en.labels],
        })

    # Which (table, column) pairs are single-column unique (sole PK or single-col unique index)?
    unique_cols: set[tuple[str, str]] = set()
    for t in introspected.tables:
        if len(t.primary_key) == 1:
            unique_cols.add((t.name, t.primary_key[0]))
    for ix in introspected.indexes:
        if ix.unique and len(ix.columns) == 1:
            unique_cols.add((ix.table, ix.columns[0]))

    confident = 0
    ambiguous = 0
    suggestions: list[dict[str, Any]] = []

    col_id: dict[tuple[str, str], str] = {}  # (table, column) → field id
    table_id: dict[str, str] = {}
    tables_out: list[dict[str, Any]] = []
    checks_meta: list[dict[str, Any]] = []

    for t in introspected.tables:
        tid = _sid("tbl", t.name)
        table_id[t.name] = tid
        pk_set = set(t.primary_key)
        fields_out: list[dict[str, Any]] = []
        for col in t.columns:
            fid = _sid("fld", t.name, col.name)
            col_id[(t.name, col.name)] = fid
            is_pk = col.name in pk_set
            is_unique = (t.name, col.name) in unique_cols
            phys = _physical_of(col)
            field: dict[str, Any] = {
                "id": fid, "name": col.name,
                "nullable": col.nullable and not is_pk,
            }

            if col.data_type.strip().lower() == "user-defined" and col.udt_name in enum_name_to_id:
                # A column backed by a Postgres enum type → an enum field referencing that enum.
                field["semanticType"] = "enum"
                field["enumId"] = enum_name_to_id[col.udt_name]
                confident += 1
            else:
                inferred = infer_semantic_type(phys["type"], col.name, unique=is_unique)
                stype = inferred.semantic_type
                confidence = inferred.confidence
                # serial / identity → an integer key that self-populates (spec §1.2).
                if _is_autoincrement(col):
                    stype = "big_integer" if phys["type"] in {"bigint", "int8"} else "integer"
                    field["autoIncrement"] = True
                    confidence = 0.9
                field["semanticType"] = stype
                # Preserve the *real* physical type when it differs from the semantic default, so the
                # import is faithful and round-trips (e.g. varchar(100), numeric(10,2)).
                override = _physical_override(phys, stype, registry)
                if override is not None:
                    field["overrides"] = {"physical": override}
                if confidence < _AMBIGUOUS_BELOW or phys["type"] in _AMBIGUOUS_PHYSICAL:
                    ambiguous += 1
                    suggestions.append({
                        "table": t.name, "column": col.name,
                        "physicalType": _render_phys(phys), "suggestedType": stype,
                        "confidence": round(confidence, 2),
                        "reason": "low-confidence reverse-inference; confirm the semantic type",
                    })
                else:
                    confident += 1

            if is_pk:
                field["isPrimaryKey"] = True
            if col.default is not None and not _is_autoincrement(col):
                literal = _default_literal(col.default)
                if literal is not None:
                    field["default"] = literal
            fields_out.append(field)

        table_entry: dict[str, Any] = {"id": tid, "name": t.name, "kind": "normal", "fields": fields_out}
        tables_out.append(table_entry)
        for chk in t.checks:
            checks_meta.append({"table": t.name, "name": chk["name"], "clause": chk["clause"]})

    relations_out = _build_relations(introspected, table_id, col_id, unique_cols, suggestions)
    _mark_pivots(introspected, tables_out, table_id, relations_out, suggestions)

    indexes_out: list[dict[str, Any]] = []
    for ix in introspected.indexes:
        cols = [col_id[(ix.table, c)] for c in ix.columns if (ix.table, c) in col_id]
        if not cols:
            continue
        indexes_out.append({
            "id": _sid("idx", ix.table, ix.name), "tableId": table_id[ix.table],
            "columns": cols, "unique": ix.unique,
        })

    schema: dict[str, Any] = {
        "formatVersion": CURRENT_FORMAT_VERSION,
        "meta": {"name": name, "databaseType": "postgres", "defaultDriver": "postgres"},
        "logical": {
            "tables": sorted(tables_out, key=lambda t: t["name"]),
            "relations": sorted(relations_out, key=lambda r: r["id"]),
        },
    }
    if enums_out:
        schema["logical"]["enums"] = sorted(enums_out, key=lambda e: e["id"])
    if indexes_out:
        schema["physical"] = {"indexes": sorted(indexes_out, key=lambda i: i["id"])}
    if checks_meta:
        schema["extensions"] = {"imported": {"checks": sorted(
            checks_meta, key=lambda c: (c["table"], c["name"]))}}

    # Validate but never crash: a real database that breaks our quality rules (e.g. a PK-less table)
    # is reported as warnings, not an error (spec §1.5).
    structural = core_sj.validate_structure(schema)
    report = core_validation.validate(core_sj.load(schema, validate=False))
    return {
        "schema_json": schema,
        "inference": {
            "confident": confident,
            "ambiguous": ambiguous,
            "suggestions": sorted(
                suggestions, key=lambda s: (s.get("table", ""), s.get("column", s.get("relation", "")))
            ),
        },
        "validation": {"summary": report.summary, "structuralErrors": structural},
    }


def _build_relations(
    introspected: IntrospectedSchema, table_id: dict[str, str], col_id: dict[tuple[str, str], str],
    unique_cols: set[tuple[str, str]], suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild logical relations from real foreign keys (spec §1.3)."""
    relations: list[dict[str, Any]] = []
    for fk in introspected.foreign_keys:
        if fk.table not in table_id or fk.ref_table not in table_id:
            continue
        if not fk.columns:
            continue
        fk_col = fk.columns[0]
        fk_field_id = col_id.get((fk.table, fk_col))
        # Cardinality from uniqueness: a unique FK column is a one-to-one, else one-to-many.
        rtype = "one_to_one" if (fk.table, fk_col) in unique_cols else "one_to_many"
        rel: dict[str, Any] = {
            "id": _sid("rel", fk.name, fk.table, fk.ref_table),
            "name": "belongsTo", "type": rtype,
            "fromTableId": table_id[fk.table], "toTableId": table_id[fk.ref_table],
        }
        if fk_field_id:
            rel["foreignKeyFieldId"] = fk_field_id
        if fk.on_delete:
            rel["onDelete"] = fk.on_delete
        if fk.on_update:
            rel["onUpdate"] = fk.on_update
        relations.append(rel)
    return relations


def _mark_pivots(
    introspected: IntrospectedSchema, tables_out: list[dict[str, Any]], table_id: dict[str, str],
    relations_out: list[dict[str, Any]], suggestions: list[dict[str, Any]],
) -> None:
    """A table with exactly two single-column FKs whose composite PK is those two columns is a pivot
    → mark it ``kind=pivot`` and *suggest* a many-to-many between the two referenced tables (§1.3)."""
    fk_by_table: dict[str, list[IntrospectedForeignKey]] = {}
    for fk in introspected.foreign_keys:
        if len(fk.columns) == 1:
            fk_by_table.setdefault(fk.table, []).append(fk)
    table_entry = {t["name"]: t for t in tables_out}
    for t in introspected.tables:
        fks = fk_by_table.get(t.name, [])
        if len(fks) != 2:
            continue
        fk_cols = {fk.columns[0] for fk in fks}
        if set(t.primary_key) != fk_cols or len(t.primary_key) != 2:
            continue
        table_entry[t.name]["kind"] = "pivot"
        left, right = sorted(fks, key=lambda f: f.name)
        relations_out.append({
            "id": _sid("rel", "m2m", t.name),
            "name": "belongsToMany", "type": "many_to_many",
            "fromTableId": table_id[left.ref_table], "toTableId": table_id[right.ref_table],
            "throughTableId": table_id[t.name],
        })
        suggestions.append({
            "relation": f"{left.ref_table}<->{right.ref_table}",
            "via": t.name, "suggestedType": "many_to_many", "confidence": 0.7,
            "reason": f"{t.name} looks like a pivot table (two FKs, composite PK); confirm the m2m",
        })


def _physical_override(phys: dict[str, Any], semantic_type: str, reg: TypeRegistry) -> dict[str, Any] | None:
    """Return a physical override iff the real column type differs from the semantic default."""
    if not reg.has(semantic_type):
        return None
    default = reg.get(semantic_type).physical_for("postgres").model_dump(exclude_none=True)
    if _norm_phys(phys) == _norm_phys(default):
        return None
    return phys


def _norm_phys(p: dict[str, Any]) -> tuple:
    return (p.get("type"), p.get("length"), p.get("precision"), p.get("scale"), p.get("dimension"))


def _render_phys(p: dict[str, Any]) -> str:
    base = str(p.get("type", "text"))
    if "length" in p:
        return f"{base}({p['length']})"
    if "precision" in p:
        return f"{base}({p['precision']},{p.get('scale', 0)})"
    return base


def _default_literal(raw: str) -> str | int | float | bool | None:
    """Best-effort parse of a Postgres column default into a JSON-able literal (or keep the expr)."""
    text = raw.strip()
    # Strip a trailing cast (``'active'::text`` → ``'active'``) for readability.
    if "::" in text:
        text = text.split("::", 1)[0].strip()
    low = text.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null"}:
        return None
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    # Function defaults (now(), gen_random_uuid(), nextval(...)) are passed through verbatim.
    return text


# ==================================================================================================
# Optional LLM enrichment (the secondary layer — only ambiguous cases; AD-5: still a suggestion).
# ==================================================================================================
async def enrich_ambiguous(result: dict[str, Any], llm: Any, *, reg: TypeRegistry | None = None) -> dict[str, Any]:
    """Best-effort: ask an LLM to refine the *ambiguous* reverse-inferences (spec §1.2).

    Mutates each ambiguous suggestion with an ``llmSuggestion`` (a registered semantic type) when the
    model proposes a sensible one. It is **never** applied automatically — the human still confirms.
    Any failure leaves the deterministic result untouched, so the import never depends on a model.
    """
    registry = reg or DEFAULT_REGISTRY
    suggestions = result.get("inference", {}).get("suggestions", [])
    pending = [s for s in suggestions if "column" in s]
    if llm is None or not pending:
        return result
    try:
        prompt = (
            "For each database column below, reply with one semantic type from this list: "
            f"{', '.join(registry.ids())}. Output a JSON object mapping 'table.column' to the type.\n"
            + "\n".join(f"- {s['table']}.{s['column']} (physical {s['physicalType']})" for s in pending)
        )
        reply = await llm.complete("You are a database architect. Output JSON only.", prompt)
        if isinstance(reply, dict):
            for s in pending:
                key = f"{s['table']}.{s['column']}"
                proposed = reply.get(key)
                if isinstance(proposed, str) and registry.has(proposed):
                    s["llmSuggestion"] = proposed
    except Exception:  # noqa: BLE001 - enrichment is best-effort; never break the deterministic import
        return result
    return result
