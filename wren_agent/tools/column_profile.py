"""Column fill-rate profiling shared by core_model and warehouse exploration.

Core modeling copies source columns faithfully — including DATE columns that
are 100% NULL in ODS (task bb58438f: `inventory_date`). Warehouse exploration
then treats the column name as a usable time grain, Grill asks how to extract
a month from it, and the DWS compiler adds `IS NOT NULL`, materializing empty
targets. Profiling the same date/period columns in both stages closes that
gap: explore fills ``FieldSchema.null_ratio``, prompts/grill show it, and the
harness rejects a 100%-null column as a grain *before* deploy.

Does not block core_model publication (empty source dates are a data fact,
not a modeling defect). Warehouse DWS/ADS that *use* those columns as grain
still fail harness.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Awaitable

from wren_agent.compiler.rules import PERIOD_COLUMN_NAMES
from wren_agent.models.schema import FieldSchema, TableSchema

# A column at or above this null ratio cannot be a time grain or an
# `IS NOT NULL` filter — the SELECT would be empty (task bb58438f).
UNUSABLE_NULL_RATIO: float = 0.99

_DATE_TYPE_RE = re.compile(r"date|time|timestamp", re.I)
_NAME_SUFFIXES: tuple[str, ...] = (
    "_date", "_dt", "_time", "_month", "_year", "_quarter",
)
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][\w$]*$")

_TIME_FN_RE = re.compile(
    r"(?:DATE_FORMAT|DATE_TRUNC|DATE|YEAR|MONTH|QUARTER)\s*\(\s*"
    r"(?:`?(?P<qual>[A-Za-z_][\w$]*)`?\s*\.\s*)?`?(?P<col>[A-Za-z_][\w$]*)`?",
    re.I,
)
_NOT_NULL_RE = re.compile(
    r"(?:`?(?P<qual>[A-Za-z_][\w$]*)`?\s*\.\s*)?`?(?P<col>[A-Za-z_][\w$]*)`?"
    r"\s+IS\s+NOT\s+NULL",
    re.I,
)


def is_profile_candidate(field: FieldSchema | dict) -> bool:
    """True for date-typed or period-named columns that can become a grain."""
    if isinstance(field, dict):
        name = str(field.get("name") or "")
        dtype = str(field.get("data_type") or field.get("type") or "")
    else:
        name = str(getattr(field, "name", "") or "")
        dtype = str(getattr(field, "data_type", "") or "")
    if not name or not _SAFE_IDENT_RE.match(name):
        return False
    if _DATE_TYPE_RE.search(dtype):
        return True
    low = name.lower()
    if low in PERIOD_COLUMN_NAMES:
        return True
    if low in ("create_time", "update_time", "created_at", "updated_at"):
        return True
    return any(low.endswith(suf) for suf in _NAME_SUFFIXES)


def is_unusable_grain(null_ratio: float | None) -> bool:
    return null_ratio is not None and null_ratio >= UNUSABLE_NULL_RATIO


def format_null_mark(null_ratio: float | None) -> str:
    """Prompt annotation for one column. Empty string when unprofiled."""
    if null_ratio is None:
        return ""
    pct = f"{null_ratio:.0%}"
    if is_unusable_grain(null_ratio):
        return (
            f" [null_rate={pct} — EMPTY, do NOT use as grain "
            f"or IS NOT NULL filter]"
        )
    return f" [null_rate={pct}]"


def populated_alternatives(fields: list[Any], *, exclude: str = "") -> list[str]:
    """Date/period columns on the same table that still have values."""
    names: list[str] = []
    excl = exclude.lower()
    for field in fields:
        if not is_profile_candidate(field):
            continue
        if isinstance(field, dict):
            name = str(field.get("name") or "")
            ratio = field.get("null_ratio")
        else:
            name = str(getattr(field, "name", "") or "")
            ratio = getattr(field, "null_ratio", None)
        if not name or name.lower() == excl:
            continue
        if ratio is None:
            continue
        if not is_unusable_grain(float(ratio)):
            names.append(f"{name} (null_rate={float(ratio):.0%})")
    return names


async def attach_null_ratios(
    table: TableSchema,
    query_one: Callable[[str], Awaitable[dict | None]],
) -> None:
    """Fill ``null_ratio`` on date/period columns with one COUNT query.

    Failures are swallowed by the caller — exploration must not die because
    a single table's profile query failed.
    """
    cols = [f for f in (table.fields or []) if is_profile_candidate(f)]
    if not cols:
        return
    schema = (table.schema_name or "").strip()
    if schema and _SAFE_IDENT_RE.match(schema):
        target = f"`{schema}`.`{table.name}`"
    else:
        target = f"`{table.name}`"
    parts = ["COUNT(*) AS `__n`"]
    for i, field in enumerate(cols):
        parts.append(
            f"SUM(CASE WHEN `{field.name}` IS NULL THEN 1 ELSE 0 END) AS `n{i}`"
        )
    row = await query_one(f"SELECT {', '.join(parts)} FROM {target}")
    if not row:
        return
    total = int(row.get("__n") or 0)
    if total <= 0:
        for field in cols:
            field.null_ratio = 0.0
        return
    for i, field in enumerate(cols):
        field.null_ratio = int(row.get(f"n{i}") or 0) / total


def grain_columns_in_sql(sql: str) -> list[tuple[str, str]]:
    """(qualifier_or_empty, column) used as a time grain or NOT NULL filter."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rx in (_TIME_FN_RE, _NOT_NULL_RE):
        for match in rx.finditer(sql or ""):
            qual = (match.group("qual") or "").lower()
            col = (match.group("col") or "").lower()
            if not col or (qual, col) in seen:
                continue
            seen.add((qual, col))
            found.append((qual, col))
    return found
