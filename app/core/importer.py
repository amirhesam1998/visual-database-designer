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

from app.core import drivers as core_drivers
from app.core import schema_json as core_sj
from app.core import validation as core_validation
from app.core.drivers.base import (
    IntrospectedColumn,
    IntrospectedEnum,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedSchema,
    IntrospectedTable,
)
from app.core.schema_json import CURRENT_FORMAT_VERSION
from app.core.type_system import DEFAULT_REGISTRY, TypeRegistry, infer_semantic_type

# The introspected data models + per-driver I/O now live in :mod:`app.core.drivers`; they are
# re-exported here so the importer's long-standing public surface is unchanged (multi-driver §0).
__all__ = [
    "IntrospectedColumn", "IntrospectedTable", "IntrospectedForeignKey", "IntrospectedIndex",
    "IntrospectedEnum", "IntrospectedSchema", "introspect_postgres", "introspect_mysql",
    "introspect_sqlserver", "apply_sql", "split_sql", "import_sql_via_shadow", "build_schema_json",
    "enrich_ambiguous",
]

# Confidence below which a reverse-inference is treated as "ambiguous" and surfaced for human
# confirmation (AD-5) rather than silently trusted.
_AMBIGUOUS_BELOW = 0.75
# Physical types that are genuinely ambiguous regardless of confidence (free-form blobs) — the spec
# calls these out as the LLM layer's job to enrich (§1.2).
_AMBIGUOUS_PHYSICAL = {"text", "json", "jsonb"}


# ==================================================================================================
# Introspection (impure) — delegated to the per-driver implementations (multi-driver milestone §0).
# The queries + connection live in :mod:`app.core.drivers`; these thin wrappers keep the importer's
# public surface stable and let callers pick a database.
# ==================================================================================================
def introspect_postgres(dsn: str, *, schema: str = "public") -> IntrospectedSchema:
    """Read a live Postgres schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    return core_drivers.get_driver("postgres").introspect(dsn, schema=schema)


def introspect_mysql(dsn: str, *, schema: str | None = None) -> IntrospectedSchema:
    """Read a live MySQL/MariaDB schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    return core_drivers.get_driver("mysql").introspect(dsn, schema=schema)


def introspect_sqlserver(dsn: str, *, schema: str | None = None) -> IntrospectedSchema:
    """Read a live SQL Server schema into :class:`IntrospectedSchema` (impure; needs a driver)."""
    return core_drivers.get_driver("sqlserver").introspect(dsn, schema=schema)


def split_sql(text: str) -> list[str]:
    """Split a raw-SQL dump into executable statements, stripping comments.

    Real dumps (mysqldump, **phpMyAdmin**) interleave comments with statements: each ``CREATE TABLE``
    is preceded by a ``-- Table structure for table ...`` block. A naive "skip lines starting with
    ``--``" therefore drops the whole following statement — which is why a phpMyAdmin import created no
    tables and then failed on the first ``ALTER TABLE`` (the root cause this milestone fixes). So we
    tokenise properly and **remove comments** while respecting quoting:

    * line comments ``-- `` (dash-dash-space / EOL) and ``#`` (MySQL), and block comments ``/* ... */``
      (including ``/*! ... */`` version-gated comments) are stripped;
    * string literals (``'...'`` / ``"..."`` with ``\\`` and doubled-quote escapes), backtick
      identifiers and Postgres dollar-quoted blocks (``$$``/``$tag$``) are honoured so a ``;`` inside
      them never splits a statement.

    Comments are dropped rather than executed: the dump is applied to a throwaway *shadow* database and
    then introspected, so charset/version pragmas are irrelevant to the resulting schema.
    """
    statements: list[str] = []
    buf: list[str] = []
    n = len(text)
    i = 0
    in_single = in_double = in_back = False
    dollar_tag: str | None = None

    def flush() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            statements.append(stmt)
        buf.clear()

    while i < n:
        ch = text[i]
        if dollar_tag:                                  # inside $tag$ ... $tag$ (Postgres)
            if text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_single or in_double:                      # inside a string literal
            quote = "'" if in_single else '"'
            buf.append(ch)
            if ch == "\\" and i + 1 < n:                # backslash escape (MySQL strings)
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if text[i + 1:i + 2] == quote:          # doubled-quote escape ('' or "")
                    buf.append(quote)
                    i += 2
                    continue
                in_single = in_double = False
            i += 1
            continue
        if in_back:                                     # inside a `backtick` identifier
            buf.append(ch)
            if ch == "`":
                in_back = False
            i += 1
            continue
        # --- not inside any quoted region: strip comments, split on ;, or open a quote ---
        if ch == "-" and text[i + 1:i + 2] == "-" and (i + 2 >= n or text[i + 2] in " \t\r\n"):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "#":                                   # MySQL line comment
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and text[i + 1:i + 2] == "*":      # block comment, incl. /*! ... */
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_back = True
        elif ch == "$":
            end = text.find("$", i + 1)
            if end != -1 and (text[i + 1:end].isidentifier() or i + 1 == end):
                dollar_tag = text[i:end + 1]
                buf.append(dollar_tag)
                i = end + 1
                continue
        elif ch == ";":
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return statements


def apply_sql(dsn: str, statements: list[str], *, driver: str = "postgres") -> None:
    """Apply raw-SQL statements in order to a (shadow) database — Leg B builder (impure; spec §2.2).

    Delegated to the driver (Postgres runs autocommit so ``CREATE INDEX CONCURRENTLY`` works; MySQL
    runs each statement autocommitting too) — adding a database never touches this seam.
    """
    core_drivers.get_driver(driver).apply_sql(dsn, statements)


def import_sql_via_shadow(
    sql: str, shadow_dsn: str, *, name: str = "imported", schema: str | None = None,
    reset: bool = True, driver: str = "postgres",
) -> dict[str, Any]:
    """Import a raw SQL/DDL dump by applying it to a **shadow database**, then introspecting it.

    This is the file-import path (database-connection milestone §2; multi-driver §2). It reuses the
    very same chain a live import does — ``split_sql`` → ``apply_sql`` → ``introspect`` →
    :func:`build_schema_json` — instead of parsing SQL in the front-end, so a uuid FK still comes back
    as ``uuid`` (Postgres) / ``CHAR(36)`` → ``uuid`` (MySQL) and the result is the identical layered,
    Stable-ID ``schema_json``. The shadow database is reset first so the import reflects only this
    dump, and using a temporary database keeps the user's real database untouched (import + compare only).
    """
    drv = core_drivers.get_driver(driver)
    if reset:
        drv.reset(shadow_dsn, schema=schema)
    drv.apply_sql(shadow_dsn, split_sql(sql))
    return build_schema_json(drv.introspect(shadow_dsn, schema=schema), name=name, driver=driver)


# ==================================================================================================
# Stable-ID minting (deterministic — same name → same id, so two imports are byte-identical).
# ==================================================================================================
def _sid(prefix: str, *parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()  # 12 hex chars
    return f"{prefix}_{digest}"


# ==================================================================================================
# Build (pure, deterministic): IntrospectedSchema → schema_json + inference + validation report.
# ==================================================================================================
def build_schema_json(
    introspected: IntrospectedSchema, *, name: str = "imported", reg: TypeRegistry | None = None,
    driver: str = "postgres",
) -> dict[str, Any]:
    """Turn introspected data into ``{schema_json, inference, validation}`` (pure, deterministic).

    The reverse type map (native column → physical spec + semantic hint) and the auto-increment test
    are owned by the ``driver`` (multi-driver milestone §1/§2); everything else — Stable IDs,
    relations, pivots, the ambiguous-suggestion bookkeeping — is shared and driver-agnostic.
    """
    registry = reg or DEFAULT_REGISTRY
    drv = core_drivers.get_driver(driver)

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
            phys = drv.column_physical(col)
            field: dict[str, Any] = {
                "id": fid, "name": col.name,
                "nullable": col.nullable and not is_pk,
            }

            if col.data_type.strip().lower() == "user-defined" and col.udt_name in enum_name_to_id:
                # A column backed by a native enum type (Postgres type / MySQL inline ENUM) → an enum
                # field referencing the reusable enum we rebuilt.
                field["semanticType"] = "enum"
                field["enumId"] = enum_name_to_id[col.udt_name]
                confident += 1
            else:
                inferred = infer_semantic_type(phys["type"], col.name, unique=is_unique)
                stype = inferred.semantic_type
                confidence = inferred.confidence
                # A driver-specific reverse rule (e.g. MySQL CHAR(36) → uuid, the FK lesson) wins over
                # the generic inference and is treated as confident (spec §1/§2).
                hint = drv.semantic_override(col, phys)
                if hint is not None:
                    stype = hint
                    confidence = 0.9
                # serial / identity → an integer key that self-populates (spec §1.2).
                if drv.is_autoincrement(col):
                    stype = "big_integer" if phys["type"] in {"bigint", "int8"} else "integer"
                    field["autoIncrement"] = True
                    confidence = 0.9
                field["semanticType"] = stype
                # Preserve the *real* physical type when it differs from the semantic default, so the
                # import is faithful and round-trips (e.g. varchar(100), numeric(10,2)).
                override = _physical_override(phys, stype, registry, driver)
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
            if col.default is not None and not drv.is_autoincrement(col):
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
        "meta": {"name": name, "databaseType": drv.name, "defaultDriver": drv.name},
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


def _physical_override(
    phys: dict[str, Any], semantic_type: str, reg: TypeRegistry, driver: str = "postgres",
) -> dict[str, Any] | None:
    """Return a physical override iff the real column type differs from the semantic default *for this
    driver*. Driver-aware so a faithful round-trip holds on each database (e.g. a MySQL CHAR(36)
    matches uuid's MySQL default and needs no override, while a varchar(100) still does)."""
    if not reg.has(semantic_type):
        return None
    default = reg.get(semantic_type).physical_for(driver).model_dump(exclude_none=True)
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
