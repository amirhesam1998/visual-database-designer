"""Test-suite guards (Milestone 0).

The repo's default ``python`` is 3.10 with none of the module dependencies installed; the tests must
run on the dedicated SDK virtualenv (``packages/module-sdk-python/.venv``, Python 3.12 + deps).
Running on the wrong interpreter would *silently* collect zero tests and report a false green, so we
FAIL LOUDLY at collection time instead of skipping (spec §2).
"""

from __future__ import annotations

import importlib.util
import sys

_MIN_PYTHON = (3, 12)
_REQUIRED = ("aiarch_module_sdk", "jsonschema", "pydantic", "fastapi")


def _fail(message: str) -> None:
    raise RuntimeError(
        f"{message}\nRun the suite with the SDK virtualenv:\n"
        r"  packages\module-sdk-python\.venv\Scripts\python.exe -m pytest"
    )


if sys.version_info < _MIN_PYTHON:
    _fail(
        f"these tests require Python >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}, "
        f"but the active interpreter is {sys.version_info[0]}.{sys.version_info[1]}."
    )

_missing = [name for name in _REQUIRED if importlib.util.find_spec(name) is None]
if _missing:
    _fail(f"missing required dependencies {_missing}; this is the wrong interpreter.")
