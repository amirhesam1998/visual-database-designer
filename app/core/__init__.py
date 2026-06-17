"""Visual Database Designer — pure, UI-independent Core (AD-4: Core-first).

This package is the deterministic source of truth described in ``docs/README.md`` and the
``docs/spec-*.md`` files. It depends only on pydantic / jsonschema (no FastAPI, no UI) so the
exact same logic powers the UI, the CLI/headless mode, the embedded component and the AI-SaaS
pipeline integration.

Subsystems, in the mandated build order (``docs/README.md`` §4):

* :mod:`app.core.ids`            — Stable IDs                     (AD-1)
* :mod:`app.core.schema_json`    — layered, versioned schema_json (AD-3)
* :mod:`app.core.type_system`    — two-layer Type System          (AD-2)
* :mod:`app.core.validation`     — deterministic Validation Engine
* :mod:`app.core.diff`           — id-based semantic Diff Engine
* :mod:`app.core.risk`           — Migration Risk Analyzer
* :mod:`app.core.state_machine`  — State Machine Designer

Everything here is deterministic; an LLM only ever *suggests* (AD-5) and never lives in Core.

Submodules are imported explicitly (``from app.core import schema_json``) rather than eagerly here,
so each subsystem stays independently importable and the build order has no import-cycle traps.
"""

from __future__ import annotations
