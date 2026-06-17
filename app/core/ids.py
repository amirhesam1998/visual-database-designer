"""Stable IDs — implementation of AD-1.

Every table/field/relation/index/enum/state-machine/state/transition gets an immutable ``id`` at
creation time. All internal references use the id, never the ``name`` — so a rename is a first-class
operation (not drop+add), comments stay anchored, and diff/merge are semantic. See
``docs/00-architecture-decisions.md`` (AD-1) and ``docs/spec-schema-json-format.md`` §8.

Format: ``<type-prefix>_<ULID>`` where the body is a Crockford-base32 ULID (time-ordered, so ids
sort by creation time). The JSON Schema enforces ``^(tbl|fld|rel|idx|enm|sm|stt|trn)_[0-9A-Za-z._-]{4,}$``.
"""

from __future__ import annotations

import os
import re
import time
from typing import Final

# Prefix → entity type. The JSON Schema (``docs/schema_json.schema.json`` $defs.id) pins this exact set.
PREFIXES: Final[dict[str, str]] = {
    "tbl": "table",
    "fld": "field",
    "rel": "relation",
    "idx": "index",
    "enm": "enum",
    "sm": "stateMachine",
    "stt": "state",
    "trn": "transition",
}

ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(tbl|fld|rel|idx|enm|sm|stt|trn)_[0-9A-Za-z._-]{4,}$")

# Crockford base32 alphabet (excludes I, L, O, U to avoid ambiguity) — what a canonical ULID uses.
_CROCKFORD: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def generate_ulid() -> str:
    """A 26-char Crockford-base32 ULID: 48-bit millisecond timestamp + 80 random bits."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    return _encode_crockford(ms, 10) + _encode_crockford(rand, 16)


def new_id(prefix: str) -> str:
    """Mint a fresh stable id for ``prefix`` (one of :data:`PREFIXES`)."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}; expected one of {sorted(PREFIXES)}")
    return f"{prefix}_{generate_ulid()}"


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def id_prefix(value: str) -> str | None:
    """Return the type prefix of an id (``'tbl_01...' -> 'tbl'``) or ``None`` if malformed."""
    if not is_valid_id(value):
        return None
    return value.split("_", 1)[0]


def id_type(value: str) -> str | None:
    """Return the human entity type of an id (``'fld_01...' -> 'field'``) or ``None``."""
    prefix = id_prefix(value)
    return PREFIXES.get(prefix) if prefix else None
