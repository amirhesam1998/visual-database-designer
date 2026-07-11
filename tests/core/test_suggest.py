"""Unit tests — AI Suggest pipeline (Milestone 1 §9).

The LLM-free heuristic is deterministic and domain-aware; the LLM branch is normalised through the
same deterministic pipeline (Stable IDs assigned, unknown types resolved, PK ensured, hallucinated
relations dropped); any LLM failure degrades to the heuristic; every suggestion is structurally
valid and is returned (never applied).
"""

from __future__ import annotations

import asyncio

from app.core import schema_json as sj
from app.core import validation as v
from app.core.suggest import suggest_schema


def _run(coro):
    return asyncio.run(coro)


class _FakeLLM:
    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail

    async def complete(self, system, user, **kw):
        if self.fail:
            raise RuntimeError("model down")
        return self.payload


# --- heuristic (no LLM) ---------------------------------------------------------------------------


def test_heuristic_is_deterministic():
    a = _run(suggest_schema("a shop with users orders and payments"))
    b = _run(suggest_schema("a shop with users orders and payments"))
    assert a == b
    assert a["source"] == "heuristic"


def test_heuristic_detects_ecommerce_entities():
    out = _run(suggest_schema("فروشگاه با کاربران، سفارش‌ها و پرداخت"))
    names = [t["name"] for t in out["suggestion"]["logical"]["tables"]]
    assert names == ["users", "orders", "payments"]
    assert len(out["suggestion"]["logical"]["relations"]) == 2  # orders→users, payments→orders


def test_unknown_prd_falls_back_to_generic_table():
    out = _run(suggest_schema("something totally unrelated to data"))
    assert [t["name"] for t in out["suggestion"]["logical"]["tables"]] == ["items"]


def test_education_prd_yields_a_multi_table_schema_no_llm():
    """Bug §1 (real usage): "build an online course education platform" produced a single generic
    table when no LLM was configured. The education bundle must now seed a coherent multi-table schema
    (courses, students, enrollments, lessons, instructors) with relations — deterministically."""
    out = _run(suggest_schema("build an online course education platform for a website"))
    assert out["source"] == "heuristic"
    names = {t["name"] for t in out["suggestion"]["logical"]["tables"]}
    assert {"courses", "students", "enrollments", "lessons", "instructors"} <= names
    # enrollments wires student_id→students and course_id→courses; lessons→courses; courses→instructors.
    assert len(out["suggestion"]["logical"]["relations"]) >= 4
    schema = sj.load(out["suggestion"])  # raises if structurally invalid
    assert v.validate(schema).valid


def test_suggestion_is_structurally_and_referentially_valid():
    out = _run(suggest_schema("blog with posts and comments and users"))
    schema = sj.load(out["suggestion"])  # raises if structurally invalid
    assert v.validate(schema).valid


def test_diff_from_current_is_the_full_creation():
    out = _run(suggest_schema("shop with users"))
    assert out["diffFromCurrent"]  # empty → suggestion produces add_table ops
    assert any(o["op"] == "add_table" for o in out["diffFromCurrent"])


# --- LLM branch -----------------------------------------------------------------------------------


def test_llm_output_is_normalised():
    payload = {
        "formatVersion": "1.0.0",
        "logical": {
            "tables": [
                {"name": "authors", "fields": [{"name": "name", "semanticType": "string"},
                                               {"name": "bio", "semanticType": "weirdtype"}]},
                {"name": "books", "fields": [{"name": "title", "semanticType": "string"},
                                             {"name": "author_id", "semanticType": "foreign_key"}]},
            ],
            "relations": [
                {"type": "one_to_many", "fromTable": "books", "toTable": "authors"},
                {"type": "one_to_many", "fromTable": "books", "toTable": "ghost"},  # hallucinated
            ],
        },
    }
    out = _run(suggest_schema("books and authors", llm=_FakeLLM(payload)))
    assert out["source"] == "llm"
    schema = sj.load(out["suggestion"])
    authors = next(t for t in schema.logical.tables if t.name == "authors")
    assert authors.primary_keys()  # a PK was injected
    bio = next(f for f in authors.fields if f.name == "bio")
    assert bio.semantic_type == "string"  # unknown 'weirdtype' resolved deterministically
    assert len(schema.logical.relations) == 1  # the 'ghost' relation was dropped


def test_llm_failure_degrades_to_heuristic():
    out = _run(suggest_schema("shop with users and orders", llm=_FakeLLM(fail=True)))
    assert out["source"] == "heuristic"
    assert [t["name"] for t in out["suggestion"]["logical"]["tables"]] == ["users", "orders"]


def test_llm_garbage_degrades_to_heuristic():
    out = _run(suggest_schema("shop with users", llm=_FakeLLM({"not": "a schema"})))
    assert out["source"] == "heuristic"
