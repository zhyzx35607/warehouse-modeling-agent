"""Try several Doris DML patches for operator/cast errors (task 60a570e6).

``||`` is boolean OR in Doris. A string literal in that chain throws
``can't cast to bool in strict mode``. The repair skill lists candidate
families; this module applies them one per revision round instead of
hard-coding a single rewrite.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import json

from wren_agent.compiler.render import rewrite_pipe_concat_sql

logger = logging.getLogger(__name__)

_CAST_BOOL_RE = re.compile(r"cast to bool", re.I)
_CEIL_HEAD_RE = re.compile(r"\bCEIL\s*\(", re.I)
_CONCAT_HEAD_RE = re.compile(r"\bCONCAT\s*\(", re.I)
_ALREADY_CAST_CEIL_RE = re.compile(r"CAST\s*\(\s*CEIL\s*\(", re.I)
_SELECT_AS_RE = re.compile(r"\s+AS\s+(`?[\w$]+`?)\s*$", re.I)


_STATS_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "logs" / "dml_repair_stats.json"
)

_TOKEN_RE = re.compile(
    r"""['\"]?([^'\"\s]+)['\"]?\s+can't\s+cast\s+to\s+bool""",
    re.I,
)


@dataclass(frozen=True)
class DmlRepairCandidate:
    label: str
    sql: str
    why: str
    p_success: float = 0.5
    score_note: str = ""


def is_operator_cast_error(text: str) -> bool:
    return bool(_CAST_BOOL_RE.search(text or ""))


def load_dml_repair_stats() -> dict[str, dict[str, int]]:
    try:
        data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def record_dml_repair_outcome(label: str, success: bool) -> None:
    if not label:
        return
    stats = load_dml_repair_stats()
    rec = stats.setdefault(label, {"wins": 0, "losses": 0})
    rec["wins" if success else "losses"] = int(rec.get("wins" if success else "losses", 0)) + 1
    rec.setdefault("wins", 0)
    rec.setdefault("losses", 0)
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        logger.warning("[doris-dml-repair] could not persist stats to %s", _STATS_PATH)


def record_pending_repair_outcome(state: dict, success: bool) -> None:
    label = str(state.get("dml_repair_pending") or "")
    if not label:
        return
    record_dml_repair_outcome(label, success)
    state["dml_repair_pending"] = ""
    logger.info("[doris-dml-repair] outcome %s → %s", label, "win" if success else "loss")


def _error_token(error: str) -> str:
    m = _TOKEN_RE.search(error or "")
    return m.group(1) if m else ""


def _heuristic_prior(label: str, sql: str, error: str) -> float:
    """How well this family matches the current error+SQL (0–1)."""
    err = (error or "").lower()
    bool_cast = "cast to bool" in err
    has_pipe = "||" in (sql or "")
    token = _error_token(error)
    quoted = bool(token) and (
        f"'{token}'" in sql or f'"{token}"' in sql
    )
    has_ceil = bool(_CEIL_HEAD_RE.search(sql or ""))
    if label == "pipe_concat":
        if bool_cast and has_pipe and quoted:
            return 0.88
        if has_pipe and quoted:
            return 0.72
        return 0.34
    if label == "concat_ceil_int":
        if has_ceil:
            return 0.55 if bool_cast else 0.40
        return 0.20
    if label == "concat_cast_varchar":
        return 0.48 if bool_cast else 0.33
    if label == "concat_ws":
        return 0.30
    if label == "pipe_as_or":
        return 0.82 if has_pipe and not quoted else 0.10
    return 0.40


def _empirical_p(label: str, stats: dict) -> float:
    rec = stats.get(label) or {}
    wins = int(rec.get("wins", 0) or 0)
    losses = int(rec.get("losses", 0) or 0)
    return (wins + 1) / (wins + losses + 2)


def score_repair_candidates(
    candidates: list[DmlRepairCandidate],
    sql: str,
    error: str,
    *,
    stats: dict | None = None,
    explain: dict[str, bool] | None = None,
) -> list[DmlRepairCandidate]:
    """Estimate P(success) per candidate and sort highest first."""
    stats = stats if stats is not None else load_dml_repair_stats()
    explain = explain or {}
    scored: list[DmlRepairCandidate] = []
    for cand in candidates:
        prior = _heuristic_prior(cand.label, sql, error)
        emp = _empirical_p(cand.label, stats)
        p = 0.55 * prior + 0.45 * emp
        note = f"prior={prior:.2f} hist={emp:.2f}"
        if cand.label in explain:
            if explain[cand.label] is False:
                p *= 0.08
                note += " explain=fail"
            else:
                p = min(0.99, p + 0.22)
                note += " explain=ok"
        scored.append(replace(
            cand,
            p_success=round(p, 4),
            score_note=note,
            why=f"{cand.why} (P≈{p:.0%})",
        ))
    scored.sort(key=lambda c: c.p_success, reverse=True)
    return scored


def generate_dml_repair_candidates(
    sql: str, error: str = "",
    *,
    stats: dict | None = None,
    explain: dict[str, bool] | None = None,
) -> list[DmlRepairCandidate]:
    """Patches for a failed INSERT/SELECT, ordered by estimated P(success)."""
    if not sql:
        return []
    out: list[DmlRepairCandidate] = []
    seen: set[str] = {sql}

    def _add(label: str, new_sql: str, why: str) -> None:
        if new_sql and new_sql not in seen:
            seen.add(new_sql)
            out.append(DmlRepairCandidate(label, new_sql, why))

    concat_sql = rewrite_pipe_concat_sql(sql)
    _add(
        "pipe_concat",
        concat_sql,
        "Doris treats || as OR; CONCAT the string-literal chain",
    )
    _add(
        "concat_ceil_int",
        _cast_ceil_as_int(concat_sql if concat_sql != sql else sql),
        "CEIL returns DOUBLE; CAST to INT before concatenating a quarter",
    )
    _add(
        "concat_cast_varchar",
        _cast_concat_args_varchar(concat_sql if concat_sql != sql else sql),
        "Force CONCAT operands to VARCHAR so mixed types are not coerced to BOOL",
    )
    _add(
        "concat_ws",
        _concat_to_concat_ws(concat_sql if concat_sql != sql else sql),
        "CONCAT_WS skips NULL pieces of a quarter/month label",
    )
    if "||" in sql and concat_sql == sql:
        _add(
            "pipe_as_or",
            re.sub(r"\|\|", " OR ", sql),
            "No string-literal operand: || was boolean OR, use the OR keyword",
        )
    use_stats = stats if stats is not None else load_dml_repair_stats()
    return score_repair_candidates(
        out, sql, error, stats=use_stats, explain=explain,
    )


def apply_next_dml_repair(
    sql: str, error: str, tried: list[str] | None = None,
    *,
    stats: dict | None = None,
    explain: dict[str, bool] | None = None,
) -> DmlRepairCandidate | None:
    skip = set(tried or [])
    for cand in generate_dml_repair_candidates(
        sql, error, stats=stats if stats is not None else load_dml_repair_stats(),
        explain=explain,
    ):
        if cand.label not in skip:
            return cand
    return None


def apply_operator_cast_candidates(
    plan: Any,
    failure: Any,
    tried: dict[str, list[str]],
    *,
    stats: dict | None = None,
    explain: dict[str, bool] | None = None,
) -> str:
    """Patch the highest-P untried DML artifact. Returns the label applied."""
    layer = str(getattr(failure, "layer", "") or "").upper()
    detail = " ".join(
        str(f.get("detail", "")) for f in (getattr(failure, "failures", None) or [])
    )
    if not is_operator_cast_error(detail) and not _failure_is_operator_cast(failure):
        return ""
    applied = ""
    for art in getattr(plan, "sql_artifacts", None) or []:
        if str(getattr(art, "layer", "")).upper() != layer:
            continue
        if not str(getattr(art, "sql_type", "")).startswith("DML"):
            continue
        sql = getattr(art, "sql", "") or ""
        if "||" not in sql and "CONCAT(" not in sql.upper():
            continue
        key = str(getattr(art, "target_table", "") or art.sql_type)
        cand = apply_next_dml_repair(
            sql, detail, tried.get(key, []),
            stats=stats if stats is not None else load_dml_repair_stats(),
            explain=explain,
        )
        if cand is None:
            continue
        art.sql = cand.sql
        tried.setdefault(key, []).append(cand.label)
        logger.info(
            "[doris-dml-repair] %s → %s P≈%.0f%% %s (%s)",
            key, cand.label, 100 * cand.p_success, cand.score_note, cand.why,
        )
        applied = cand.label
        break
    return applied


def insert_select_body(sql: str) -> str:
    """SELECT-list+FROM body for ``EXPLAIN SELECT <body>``."""
    m = re.search(r"\bSELECT\b", sql or "", re.I)
    if not m:
        return ""
    return (sql[m.end():] or "").strip().rstrip(";")


def patch_semantic_exprs(design: Any, label: str, error: str) -> None:
    """Keep compiler-mode source_expr in sync with the SQL patch."""
    for col in getattr(design, "columns", None) or []:
        expr = getattr(col, "source_expr", "") or ""
        if not expr:
            continue
        fake = f"SELECT {expr} AS `_x` FROM `_t`"
        cand = next(
            (c for c in generate_dml_repair_candidates(fake, error, stats={})
             if c.label == label),
            None,
        )
        if cand is None:
            continue
        new_expr = _select_item_expr(cand.sql)
        if new_expr and new_expr != expr:
            col.source_expr = new_expr


def _failure_is_operator_cast(failure: Any) -> bool:
    for f in getattr(failure, "failures", None) or []:
        if str(f.get("check", "")) == "doris_operator_cast":
            return True
        if is_operator_cast_error(str(f.get("detail", ""))):
            return True
    return False


def _select_item_expr(sql: str) -> str:
    m = re.search(r"\bSELECT\b", sql, re.I)
    if not m:
        return ""
    rest = sql[m.end():]
    from_m = re.search(r"\bFROM\b", rest, re.I)
    item = rest[:from_m.start()] if from_m else rest
    am = _SELECT_AS_RE.search(item)
    return (item[:am.start()] if am else item).strip()


def _matching_paren(sql: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"'):
            q = c
            i += 1
            while i < n:
                if sql[i] == q:
                    if q == "'" and i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _concat_calls(sql: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in _CONCAT_HEAD_RE.finditer(sql):
        open_idx = sql.find("(", m.start())
        if open_idx < 0:
            continue
        end = _matching_paren(sql, open_idx)
        spans.append((m.start(), end))
    return spans


def _split_concat_args(inner: str) -> list[str]:
    from wren_agent.compiler.render import _split_top_level
    return [p.strip() for p in _split_top_level(inner, ",") if p.strip()]


def _is_string_literal(expr: str) -> bool:
    s = (expr or "").strip()
    return len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]


def _cast_concat_args_varchar(sql: str) -> str:
    pieces: list[str] = []
    last = 0
    changed = False
    for start, end in _concat_calls(sql):
        pieces.append(sql[last:start])
        open_idx = sql.find("(", start)
        inner = sql[open_idx + 1:end - 1]
        args = _split_concat_args(inner)
        new_args = []
        for arg in args:
            if _is_string_literal(arg) or arg.upper().startswith("CAST("):
                new_args.append(arg)
            else:
                new_args.append(f"CAST({arg} AS VARCHAR)")
                changed = True
        pieces.append("CONCAT(" + ", ".join(new_args) + ")")
        last = end
    if not changed:
        return sql
    pieces.append(sql[last:])
    return "".join(pieces)


def _cast_ceil_as_int(sql: str) -> str:
    if _ALREADY_CAST_CEIL_RE.search(sql):
        return sql
    pieces: list[str] = []
    last = 0
    for m in _CEIL_HEAD_RE.finditer(sql):
        open_idx = sql.find("(", m.start())
        end = _matching_paren(sql, open_idx)
        pieces.append(sql[last:m.start()])
        pieces.append("CAST(" + sql[m.start():end] + " AS INT)")
        last = end
    if last == 0:
        return sql
    pieces.append(sql[last:])
    return "".join(pieces)


def _concat_to_concat_ws(sql: str) -> str:
    pieces: list[str] = []
    last = 0
    changed = False
    for start, end in _concat_calls(sql):
        pieces.append(sql[last:start])
        open_idx = sql.find("(", start)
        inner = sql[open_idx + 1:end - 1]
        pieces.append("CONCAT_WS('', " + inner + ")")
        last = end
        changed = True
    if not changed:
        return sql
    pieces.append(sql[last:])
    return "".join(pieces)
