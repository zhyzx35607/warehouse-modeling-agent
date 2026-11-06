"""Admit Doris scalars onto a task-scoped compiler whitelist (ADR-0018).

Permanent ``SCALAR_FUNCTIONS`` stays small. When compile refuses
``function X() not on the compile whitelist``, this module may add X to
``state["compiler_extra_functions"]`` for the current task only — after a
deny-list / known-agg / cap check and a successful ``EXPLAIN SELECT``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from wren_agent.compiler.rules import (
    AGG_FUNCTIONS,
    EXPR_KEYWORDS,
    SCALAR_FUNCTIONS,
    WINDOW_FUNCTIONS,
)
from wren_agent.compiler.render import try_compile_semantic_design
from wren_agent.models.semantic import SemanticDesign

logger = logging.getLogger(__name__)

ExplainFn = Callable[[str], Awaitable[tuple[bool, str]]]

EXTRA_FN_CAP: int = 8

DENY_FUNCTIONS: frozenset[str] = frozenset({
    "SLEEP", "BENCHMARK", "RAND", "RANDOM",
})

# Doris aggregates that are not in the core SUM/COUNT/MAX/MIN/AVG set.
# Admitting them as scalars would let wrap_dws emit MAX(GROUP_CONCAT(...)).
KNOWN_DORIS_AGGS: frozenset[str] = frozenset({
    "GROUP_CONCAT", "COLLECT_SET", "COLLECT_LIST", "ARRAY_AGG",
    "PERCENTILE", "PERCENTILE_APPROX", "NDV", "HLL_UNION", "BITMAP_UNION",
    "COUNT_IF", "ANY_VALUE", "GROUPING", "GROUPING_ID",
})

_FN_PROBLEM_RE = re.compile(
    r"function\s+([A-Za-z_][A-Za-z0-9_]*)\(\)\s+not on the compile whitelist",
    re.I,
)
_QUOTED_EXPR_RE = re.compile(r"\((['\"])(.+)\1\)\s*$")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_QUALIFIED_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
)
_BARE_IDENT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()",
)


@dataclass
class AdmitOutcome:
    admitted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    explain_error: str = ""


def unknown_functions_from_problems(problems: Iterable[str]) -> list[str]:
    """Unique unknown function names in compile-problem order."""
    seen: list[str] = []
    for text in problems or []:
        for m in _FN_PROBLEM_RE.finditer(str(text)):
            name = m.group(1).upper()
            if name not in seen:
                seen.append(name)
    return seen


def classify_admit_name(name: str) -> str:
    """``ok`` / ``deny`` / ``agg`` / ``invalid``."""
    n = (name or "").strip().upper()
    if not _NAME_RE.fullmatch(n):
        return "invalid"
    if n in DENY_FUNCTIONS:
        return "deny"
    if n in KNOWN_DORIS_AGGS:
        return "agg"
    if n in AGG_FUNCTIONS or n in SCALAR_FUNCTIONS or n in WINDOW_FUNCTIONS:
        return "ok"
    return "ok"


def stub_expr_for_explain(expr: str, *, filler: str = "1") -> str:
    """Replace column refs with ``filler``; keep function names and keywords."""
    stub = _QUALIFIED_RE.sub(filler, expr or "")

    def _bare(m: re.Match[str]) -> str:
        word = m.group(1)
        upper = word.upper()
        if upper in EXPR_KEYWORDS or upper == "NULL":
            return word
        if upper == filler.upper():
            return word
        return filler

    return _BARE_IDENT_RE.sub(_bare, stub)


def _expr_containing(
    design: SemanticDesign, name: str, problems: list[str],
) -> str:
    needle = name.upper()
    pattern = re.compile(rf"\b{re.escape(needle)}\s*\(", re.I)
    for col in design.columns or []:
        expr = (getattr(col, "source_expr", None) or "").strip()
        if expr and pattern.search(expr):
            return expr
    for text in problems or []:
        m = _QUOTED_EXPR_RE.search(str(text))
        if m and pattern.search(m.group(2)):
            return m.group(2)
    return f"{name}(1)"


async def explain_select(sql: str) -> tuple[bool, str]:
    """Run ``EXPLAIN …`` on the task's Doris FE. Errors do not admit."""
    from wren_agent.tools.doris_client import DorisClient

    try:
        client = DorisClient()
        await client.query(sql)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:400]


async def _explain_expr(expr: str, explain: ExplainFn) -> tuple[bool, str]:
    last_err = ""
    for filler in ("1", "NULL"):
        stub = stub_expr_for_explain(expr, filler=filler)
        sql = f"EXPLAIN SELECT {stub}"
        ok, err = await explain(sql)
        if ok:
            return True, ""
        last_err = err or last_err
    return False, last_err or "EXPLAIN rejected"


def _existing_extra(state: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for raw in state.get("compiler_extra_functions") or []:
        name = str(raw).strip().upper()
        if name and name not in seen:
            seen.append(name)
    return seen


async def try_admit_unknown_functions(
    state: dict[str, Any],
    design: SemanticDesign,
    problems: list[str],
    *,
    explain: ExplainFn | None = None,
) -> AdmitOutcome:
    """Maybe add unknown functions to ``state["compiler_extra_functions"]``."""
    outcome = AdmitOutcome()
    names = unknown_functions_from_problems(problems)
    if not names:
        return outcome

    probe = explain if explain is not None else explain_select
    existing = _existing_extra(state)
    product = EXTRA_FN_CAP - len(existing)
    to_admit: list[str] = []

    for name in names:
        kind = classify_admit_name(name)
        if kind != "ok":
            outcome.skipped.append(name)
            continue
        if name in existing or name in to_admit:
            continue
        if product <= 0 or len(existing) + len(to_admit) >= EXTRA_FN_CAP:
            outcome.skipped.append(name)
            continue
        expr = _expr_containing(design, name, problems)
        ok, err = await _explain_expr(expr, probe)
        if not ok:
            outcome.skipped.append(name)
            if err:
                outcome.explain_error = err
            continue
        to_admit.append(name)

    if to_admit:
        state["compiler_extra_functions"] = existing + to_admit
        outcome.admitted = list(to_admit)
        logger.info(
            "[compiler-fn-admit] task extra functions +%s → %s",
            to_admit, state["compiler_extra_functions"],
        )
    return outcome


async def compile_design_with_admit(
    state: dict[str, Any],
    design: SemanticDesign,
    **kwargs: Any,
) -> tuple[Any, list[str]]:
    """Compile; on whitelist-function refuse, EXPLAIN-admit and retry once."""
    extra = list(state.get("compiler_extra_functions") or [])
    compiled, problems = try_compile_semantic_design(
        design, extra_functions=extra, **kwargs,
    )
    if compiled is not None:
        return compiled, problems
    if not unknown_functions_from_problems(problems):
        return compiled, problems
    outcome = await try_admit_unknown_functions(state, design, problems)
    if outcome.admitted:
        extra = list(state.get("compiler_extra_functions") or [])
        compiled, problems = try_compile_semantic_design(
            design, extra_functions=extra, **kwargs,
        )
    elif outcome.explain_error:
        problems = list(problems) + [outcome.explain_error]
    return compiled, problems
