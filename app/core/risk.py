"""Migration Risk Analyzer — turns a Diff operation list into a risk-graded, safe migration plan.

This is what makes the tool production-grade (``docs/spec-migration-risk-analyzer.md``). It consumes
the *operation list* from :mod:`app.core.diff` (not raw schemas), classifies every op across four
risk dimensions, generates expand/contract safe plans + rollback + backfill for the dangerous ones,
and emits a machine report, a SARIF log and CI exit codes.

Driver-specific behaviour (PostgreSQL ``CREATE INDEX CONCURRENTLY``, ``NOT VALID`` + ``VALIDATE``;
MySQL online-DDL vs COPY) is kept as *data* (``_DRIVER_RULES``), never hard-coded assumptions, because
locking behaviour changes between versions (spec §4). Deterministic and LLM-free.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

_TYPE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*)\s*(?:\((\d+)(?:\s*,\s*(\d+))?\))?")


class RiskLevel(IntEnum):
    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> RiskLevel:
        return cls[label.strip().upper()]


# SARIF level per risk level.
_SARIF = {RiskLevel.SAFE: "note", RiskLevel.LOW: "note", RiskLevel.MEDIUM: "warning",
          RiskLevel.HIGH: "error", RiskLevel.CRITICAL: "error"}


class OperationRisk(BaseModel):
    op: str
    target: str | None = None
    level: str
    dimensions: list[str] = Field(default_factory=list)
    reversible: bool = True
    requires_backup: bool = False
    lock_impact: str | None = None
    recommended_clause: str | None = None
    explanation: dict[str, str] = Field(default_factory=dict)
    safe_plan: list[str] = Field(default_factory=list)
    rollback: str | None = None
    backfill: dict[str, Any] | None = None


class RiskReport(BaseModel):
    driver: str
    deploy_mode: str
    operations: list[OperationRisk] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    max_level: str = RiskLevel.SAFE.label
    exit_code: int = 0

    def gate(self, fail_on: str = "critical") -> bool:
        """True == the migration should be BLOCKED (its max level ≥ ``fail_on``)."""
        return RiskLevel.from_label(self.max_level) >= RiskLevel.from_label(fail_on)

    def checklist(self) -> list[str]:
        """Human pre-migration checklist (spec §7.3): destructive ops, required backups, order."""
        lines: list[str] = []
        backups = [o for o in self.operations if o.requires_backup]
        if backups:
            lines.append("Take a backup before running — irreversible operations present:")
            lines += [f"  • {o.op} {o.target or ''}".rstrip() for o in backups]
        destructive = [o for o in self.operations if RiskLevel.from_label(o.level) >= RiskLevel.HIGH]
        if destructive:
            lines.append("Manual approval required for high/critical operations:")
            lines += [f"  • [{o.level}] {o.op} {o.target or ''}".rstrip() for o in destructive]
        if not lines:
            lines.append("No destructive operations — safe to apply.")
        return lines

    def to_sarif(self) -> dict:
        rule_ids = sorted({o.op for o in self.operations})
        return {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [
                {
                    "tool": {"driver": {"name": "vdb-risk", "rules": [{"id": r} for r in rule_ids]}},
                    "results": [
                        {
                            "ruleId": o.op,
                            "level": _SARIF[RiskLevel.from_label(o.level)],
                            "message": {"text": o.explanation.get("en", o.op)},
                            "properties": {
                                "riskLevel": o.level,
                                "dimensions": o.dimensions,
                                "reversible": o.reversible,
                                "requiresBackup": o.requires_backup,
                            },
                            "locations": [{"logicalLocations": [{"name": o.target or "<schema>"}]}],
                        }
                        for o in self.operations
                    ],
                }
            ],
        }


# --------------------------------------------------------------------------------------------------
# Driver rules (data, not assumptions — spec §4).
# --------------------------------------------------------------------------------------------------
_DRIVER_RULES: dict[str, dict[str, dict[str, str]]] = {
    "postgres": {
        "set_not_null": {
            "lock_impact": "full table scan",
            "recommended_clause": "ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID; VALIDATE CONSTRAINT",
        },
        "add_index": {"lock_impact": "writes blocked unless concurrent",
                      "recommended_clause": "CREATE INDEX CONCURRENTLY (outside a transaction)"},
        "change_type": {"lock_impact": "table rewrite + ACCESS EXCLUSIVE lock", "recommended_clause": ""},
    },
    "mysql": {
        "set_not_null": {"lock_impact": "copy or inplace depending on version",
                         "recommended_clause": "ALGORITHM=INPLACE, LOCK=NONE (if supported)"},
        "add_index": {"lock_impact": "online DDL usually", "recommended_clause": "ALGORITHM=INPLACE, LOCK=NONE"},
        "change_type": {"lock_impact": "often COPY (table rebuild)", "recommended_clause": ""},
    },
}


def _driver_hint(driver: str, op: str) -> dict[str, str]:
    return _DRIVER_RULES.get(driver, {}).get(op, {})


def _parse_type(s: str | None) -> tuple[str, int | None, int | None]:
    if not s:
        return ("", None, None)
    m = _TYPE_RE.match(str(s))
    if not m:
        return (str(s).strip().lower(), None, None)
    base = m.group(1).strip().lower()
    a = int(m.group(2)) if m.group(2) else None
    b = int(m.group(3)) if m.group(3) else None
    return (base, a, b)


def _change_type_is_narrowing(from_s: str | None, to_s: str | None) -> bool:
    fb, fa, _ = _parse_type(from_s)
    tb, ta, _ = _parse_type(to_s)
    if fb != tb:
        return True  # incompatible base type change → treat as narrowing/risky
    if fa is not None and ta is not None:
        return ta < fa
    return False


# --------------------------------------------------------------------------------------------------
# Per-op classification.
# --------------------------------------------------------------------------------------------------
def _explain(fa: str, en: str) -> dict[str, str]:
    return {"fa": fa, "en": en}


def _classify(op: dict[str, Any], driver: str, mode: str) -> OperationRisk:
    name = op["op"]
    target = op.get("fieldId") or op.get("tableId") or op.get("entityId")
    hint = _driver_hint(driver, name)
    base = OperationRisk(
        op=name, target=target, level=RiskLevel.SAFE.label,
        lock_impact=hint.get("lock_impact") or None,
        recommended_clause=hint.get("recommended_clause") or None,
    )

    if name == "add_table":
        base.level = RiskLevel.SAFE.label
        base.rollback = "drop_table"
        base.explanation = _explain("ساخت جدول جدید بی‌خطر است.", "Creating a new table is safe.")
    elif name == "add_column":
        field = op.get("field") or {}
        nullable = field.get("nullable", True)
        has_default = field.get("default") is not None
        base.rollback = "drop_column"
        if nullable:
            base.level = RiskLevel.SAFE.label
            base.explanation = _explain("افزودن ستون nullable بی‌خطر است.", "Adding a nullable column is safe.")
        elif has_default:
            base.level = RiskLevel.MEDIUM.label if driver == "mysql" else RiskLevel.LOW.label
            base.explanation = _explain("ستون NOT NULL با default؛ بسته به driver ممکن است قفل کند.",
                                        "NOT NULL with a default; may lock depending on driver/version.")
        else:
            base.level = RiskLevel.HIGH.label
            base.dimensions = ["constraint", "app_break"]
            base.explanation = _explain("ستون NOT NULL بدون default روی جدول پر از داده شکست می‌خورد.",
                                        "NOT NULL without a default fails on a populated table.")
            base.safe_plan = [
                "Deploy 1: add the column as nullable; new code always writes it.",
                "Backfill existing rows in batches.",
                "Deploy 2: add the NOT NULL constraint.",
            ]
            base.backfill = _backfill(target)
    elif name == "drop_column":
        base.level = RiskLevel.HIGH.label
        base.dimensions = ["data_loss", "app_break"]
        base.reversible = False
        base.requires_backup = True
        base.rollback = "add_column (data is gone — restore from backup)"
        base.explanation = _explain("حذف ستون باعث از دست رفتن داده می‌شود.", "Dropping a column loses data.")
        base.safe_plan = [
            "Deploy 1: stop reading/writing the column in code (deprecate).",
            "Backup the table.",
            "Deploy 2: drop the column after confirming no consumers remain.",
        ]
    elif name == "rename_column":
        base.dimensions = ["app_break"]
        base.rollback = "rename_column (reverse)"
        if mode == "downtime":
            base.level = RiskLevel.MEDIUM.label
            base.explanation = _explain("rename مستقیم در downtime مجاز است.",
                                        "Direct rename is acceptable with downtime.")
        else:
            base.level = RiskLevel.MEDIUM.label
            base.explanation = _explain("rename در rolling deploy کد قدیمی را می‌شکند.",
                                        "Rename breaks old code during a rolling deploy.")
            base.safe_plan = [
                "Expand: add the new column, dual-write, backfill from the old column.",
                "Switch reads to the new column.",
                "Contract: drop the old column after all old instances are gone.",
            ]
            base.backfill = _backfill(target)
    elif name == "change_type":
        narrowing = _change_type_is_narrowing(op.get("from"), op.get("to"))
        if narrowing:
            base.level = RiskLevel.HIGH.label
            base.dimensions = ["data_loss", "constraint"]
            base.reversible = False
            base.requires_backup = True
            base.rollback = "widen back (possible data loss)"
            base.explanation = _explain("تغییر نوع باریک‌کننده ممکن است داده را قطع کند.",
                                        "Narrowing/incompatible type change may truncate data.")
            base.safe_plan = [
                "Deploy 1: add a new column with the target type (nullable).",
                "Dual-write old + new; backfill, reporting rows that don't fit.",
                "Deploy 2: read from the new column.",
                "Deploy 3: drop the old column.",
            ]
            base.backfill = _backfill(target)
        else:
            base.level = RiskLevel.LOW.label
            base.rollback = "narrow back (may lose data)"
            base.explanation = _explain("widening معمولاً امن است (بسته به driver ممکن است rewrite کند).",
                                        "Widening is usually safe (may rewrite depending on driver).")
    elif name == "change_semantic_type":
        base.level = RiskLevel.SAFE.label
        base.explanation = _explain("تغییر نوع معنایی متادیتاست؛ ریسک فیزیکی در change_type جداست.",
                                    "Semantic-type change is metadata; physical risk is the separate change_type.")
    elif name == "set_not_null":
        base.level = RiskLevel.HIGH.label
        base.dimensions = ["constraint", "lock"]
        base.rollback = "drop_not_null"
        base.explanation = _explain("NOT NULL کردن نیاز به اسکن کامل و داده‌ی بدون null دارد.",
                                    "Setting NOT NULL needs a full scan and non-null data.")
        base.safe_plan = [
            "Deploy 1: keep nullable; new code always writes a value.",
            "Backfill null rows in batches.",
            "Deploy 2: add the constraint (PG: NOT VALID then VALIDATE).",
        ]
        base.backfill = _backfill(target)
    elif name == "drop_not_null":
        base.level = RiskLevel.SAFE.label
        base.rollback = "set_not_null"
        base.explanation = _explain("nullable کردن بی‌خطر است.", "Relaxing to nullable is safe.")
    elif name == "change_default":
        base.level = RiskLevel.LOW.label
        base.rollback = "restore previous default"
        base.explanation = _explain("تغییر default کم‌خطر است.", "Changing a default is low risk.")
    elif name == "set_primary_key":
        base.level = RiskLevel.HIGH.label
        base.dimensions = ["lock", "constraint"]
        base.explanation = _explain("افزودن primary key اسکن و قفل می‌خواهد.",
                                    "Adding a primary key requires a scan and lock.")
    elif name == "add_index":
        unique = op.get("unique", False)
        base.rollback = "drop_index"
        if unique:
            base.level = RiskLevel.HIGH.label
            base.dimensions = ["lock", "constraint"]
            base.explanation = _explain("unique index روی داده‌ی تکراری شکست می‌خورد.",
                                        "A unique index fails on duplicate data.")
            base.safe_plan = ["Check for duplicates first, then build (PG: CONCURRENTLY)."]
        else:
            base.level = RiskLevel.MEDIUM.label
            base.dimensions = ["lock"]
            base.explanation = _explain("ساخت index ممکن است نوشتن را قفل کند مگر concurrent.",
                                        "Building an index can block writes unless built concurrently.")
    elif name == "drop_index":
        base.level = RiskLevel.LOW.label
        base.dimensions = ["app_break"]
        base.rollback = "add_index"
        base.explanation = _explain("حذف index ممکن است performance را خراب کند.",
                                    "Dropping an index may hurt performance.")
    elif name == "change_index":
        base.level = RiskLevel.MEDIUM.label
        base.dimensions = ["lock"]
        base.explanation = _explain("تغییر index = drop + create.", "Changing an index = drop + create.")
    elif name == "add_relation":
        base.level = RiskLevel.MEDIUM.label
        base.dimensions = ["lock", "constraint"]
        base.rollback = "drop_relation"
        base.explanation = _explain("افزودن FK داده‌ی موجود را اعتبارسنجی می‌کند.",
                                    "Adding a foreign key validates existing data.")
    elif name == "drop_relation":
        base.level = RiskLevel.LOW.label
        base.rollback = "add_relation"
        base.explanation = _explain("حذف FK کم‌خطر است.", "Dropping a foreign key is low risk.")
    elif name == "change_relation":
        base.level = RiskLevel.MEDIUM.label
        base.dimensions = ["constraint"]
        base.explanation = _explain("تغییر onDelete/onUpdate رفتار cascade را عوض می‌کند.",
                                    "Changing onDelete/onUpdate changes cascade behaviour.")
    elif name == "add_enum_value":
        base.level = RiskLevel.LOW.label
        base.explanation = _explain("افزودن مقدار enum معمولاً امن است.", "Adding an enum value is usually safe.")
    elif name in {"drop_enum_value", "drop_state"}:
        base.level = RiskLevel.HIGH.label
        base.dimensions = ["data_loss", "constraint"]
        base.reversible = False
        base.requires_backup = True
        base.explanation = _explain("حذف مقدار enum/state ردیف‌های آن مقدار را بی‌اعتبار می‌کند.",
                                    "Removing an enum value/state invalidates rows holding it.")
        base.safe_plan = ["Confirm no rows use this value (or migrate them), back up, then remove."]
    elif name == "rename_enum_value":
        base.level = RiskLevel.MEDIUM.label
        base.dimensions = ["app_break"]
        base.explanation = _explain("rename مقدار enum کد وابسته را می‌شکند.",
                                    "Renaming an enum value breaks dependent code.")
    elif name == "drop_table":
        base.level = RiskLevel.CRITICAL.label
        base.dimensions = ["data_loss", "app_break"]
        base.reversible = False
        base.requires_backup = True
        base.rollback = "create_table (data is gone — restore from backup)"
        base.explanation = _explain("حذف جدول همه‌ی داده‌ها را از بین می‌برد.",
                                    "Dropping a table destroys all of its data.")
        base.safe_plan = ["Back up the table, deprecate all consumers, then drop in a later deploy."]
    elif name in {"add_state", "add_transition"}:
        base.level = RiskLevel.LOW.label if name == "add_state" else RiskLevel.SAFE.label
        base.explanation = _explain("افزودن state/transition منطقی است.", "Adding a state/transition.")
    elif name in {"add_business_rule", "change_business_rule", "drop_business_rule", "drop_transition",
                  "change_table_meta", "change_state_machine"}:
        base.level = RiskLevel.SAFE.label
        base.explanation = _explain("تغییر معنایی/متادیتا اثر ساختاری روی DB ندارد.",
                                    "Semantic/metadata change with no structural DB impact.")
    elif name == "rename_table":
        base.level = RiskLevel.MEDIUM.label
        base.dimensions = ["app_break"]
        base.rollback = "rename_table (reverse)"
        base.explanation = _explain("rename جدول در rolling deploy کد قدیمی را می‌شکند.",
                                    "Renaming a table breaks old code during a rolling deploy.")
    else:  # pragma: no cover - unknown op falls back to a conservative warning
        base.level = RiskLevel.MEDIUM.label
        base.explanation = _explain("عملیات ناشناخته؛ به‌صورت محافظه‌کارانه medium.",
                                    "Unknown operation; conservatively medium.")
    return base


def _backfill(target: str | None) -> dict[str, Any]:
    """Idempotent, resumable, batched backfill plan (spec §9)."""
    return {
        "target": target,
        "batch_size": 1000,
        "idempotent": True,
        "resume": "track the last processed primary key; re-running skips done rows",
        "warning": "Ensure an index supports the batch predicate before backfilling a large table.",
        "estimate": "row count comes from the Cost Estimator (phase 3)",
    }


# --------------------------------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------------------------------
def analyze(operations: list[dict[str, Any]], *, driver: str = "postgres", deploy_mode: str = "rolling") -> RiskReport:
    """Classify a Diff operation list into a :class:`RiskReport`.

    ``deploy_mode`` is ``"rolling"`` (expand/contract plans) or ``"downtime"`` (direct ops allowed).
    """
    risks = [_classify(op, driver, deploy_mode) for op in operations]
    summary = {lvl.label: 0 for lvl in RiskLevel}
    max_level = RiskLevel.SAFE
    for r in risks:
        lvl = RiskLevel.from_label(r.level)
        summary[r.level] += 1
        max_level = max(max_level, lvl)
    exit_code = 2 if max_level >= RiskLevel.CRITICAL else (1 if max_level >= RiskLevel.HIGH else 0)
    return RiskReport(
        driver=driver, deploy_mode=deploy_mode, operations=risks,
        summary=summary, max_level=max_level.label, exit_code=exit_code,
    )
