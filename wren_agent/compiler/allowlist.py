"""Goal-declared source table allowlist (task 94a11f97).

Warehouse goals sometimes name a closed set of source tables
(``源表仅限 dwd_x + dim_y`` / ``只使用 dwd_x，JOIN dim_y``). The compiler
treats that set as closed-world: extra JOIN tables are refused, not remapped
onto a similar allowed name (``dim_category`` is not ``dim_sku``).

No restriction marker → ``None`` (normal warehouse may join any explored
table). Restriction marker but no explored table names in the clause →
``None`` (do not invent an empty allowlist from column tokens).
"""

from __future__ import annotations

import re

from wren_agent.compiler.rules import strip_db_prefix, strip_source_alias

# Activate only on explicit restriction language — mentioning a table in the
# goal is not a closed set.
_RESTRICT_RE = re.compile(
    r"(?:"
    r"源表\s*仅限|仅限源表|源表仅"
    r"|只使用|只允许|只能用"
    r"|只用(?!于)"
    r"|限定源表"
    r"|(?:source tables?|sources)\s+only"
    r"|only\s+use"
    r")",
    re.I,
)

_CLAUSE_RE = re.compile(
    r"(?:"
    r"源表\s*仅限|仅限源表|源表仅"
    r"|只使用|只允许|只能用"
    r"|只用(?!于)"
    r"|限定源表"
    r"|(?:source tables?|sources)\s+only"
    r"|only\s+use"
    r")\s*[:：]?\s*(.+?)"
    r"(?=。|；|;|$|，?\s*(?:不要|不得|禁止|不使用))",
    re.I | re.S,
)

_TABLE_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{3,})\b")


def parse_goal_source_allowlist(
    goal: str, explored_bare: set[str],
) -> frozenset[str] | None:
    """Return allowed bare table names, or None when the goal does not restrict.

    Tokens in the restriction clause are kept only when they match an explored
    table name (case-insensitive). ``is_current`` / prose words are ignored.
    """
    if not (goal or "").strip() or not explored_bare:
        return None
    if not _RESTRICT_RE.search(goal):
        return None
    explored_lower = {n.lower() for n in explored_bare if n}
    allowed: set[str] = set()
    clauses = _CLAUSE_RE.findall(goal)
    if not clauses:
        return None
    for clause in clauses:
        for tok in _TABLE_TOKEN_RE.findall(clause):
            low = tok.lower()
            if low in explored_lower:
                allowed.add(low)
    return frozenset(allowed) if allowed else None


def bare_source_name(ref: str) -> str:
    """``dim_gz_test.dim_sku b`` → ``dim_sku``."""
    return strip_db_prefix(strip_source_alias(ref or "")).lower().strip("`")


def source_allowlist_problems(
    design, allowed_bare: set[str] | frozenset[str] | None,
) -> list[str]:
    """Problems for ``sources`` / ``joins.right`` outside the goal allowlist."""
    if not allowed_bare:
        return []
    allowed_lower = {str(n).lower() for n in allowed_bare}
    avail = ", ".join(sorted(allowed_lower))
    problems: list[str] = []
    seen: set[str] = set()

    def check(ref: str, kind: str) -> None:
        bare = bare_source_name(ref)
        if not bare or bare in allowed_lower or bare in seen:
            return
        if bare not in allowed_lower:
            seen.add(bare)
            problems.append(
                f"{kind} `{ref}` is not in the goal source allowlist "
                f"(bare `{bare}`). Allowed tables: {avail}"
            )

    for src in getattr(design, "sources", None) or []:
        check(str(src), "source")
    for join in getattr(design, "joins", None) or []:
        if isinstance(join, dict):
            check(str(join.get("right") or ""), "JOIN")
        else:
            check(str(getattr(join, "right", "") or ""), "JOIN")
    return problems
