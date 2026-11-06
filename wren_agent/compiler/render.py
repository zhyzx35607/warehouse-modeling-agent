"""DesignCompiler entry point (ADR-0015 Phase 1).

``compile_semantic_design`` turns a semantic design into DDL + load SQL with
deterministic templates. Every ``source_expr`` is validated against a
Doris-safe expression whitelist BEFORE rendering — the compiler never emits
SQL whose expressions it cannot vouch for. Task-scoped extras
(ADR-0018 ``compiler_extra_functions``) may extend that whitelist after
EXPLAIN; they never mutate ``SCALAR_FUNCTIONS``. When an expression is
off-whitelist (or a design fails structural checks), the table is refused
instead of rendering something unverifiable.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from wren_agent.compiler.allowlist import source_allowlist_problems
from wren_agent.compiler.rules import (
    AGG_FUNCTIONS,
    EXPR_KEYWORDS,
    SCALAR_FUNCTIONS,
    WINDOW_FUNCTIONS,
    _SIMPLE_SOURCE_EXPR_RE,
    apply_composed_table_name,
    first_expr_alias,
    landing_db_for,
    normalize_table_name,
    remap_simple_source_expr,
    strip_db_prefix,
    strip_source_alias,
    wrap_dws_non_aggregated,
)
from wren_agent.compiler.templates import render_ddl, render_load_sql
from wren_agent.models.semantic import CompiledSQL, SemanticDesign

logger = logging.getLogger(__name__)

# ── Expression whitelist ────────────────────────────────────────────
# Tokenizer-level validation: an expression is allowed when it is composed
# ONLY of whitelisted function calls, identifiers, numeric literals, strings,
# and a small operator set. Anything else (LATERAL VIEW, UNNEST, subqueries,
# unknown functions, window syntax) is rejected → per-table fallback.

_ALLOWED_OPS: re.Pattern = re.compile(r"^\s*[+\-*/()<>=\s,]*$")

_TOKEN_RE = re.compile(
    r"""
    (?P<func>[A-Za-z_][A-Za-z0-9_]*)\s*\(       # function call
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*  # [alias.]column
  | (?P<number>\d+(\.\d+)?)
  | (?P<string>'(?:[^']|'')*')
  | (?P<op>[+\-*/()<>=!,.\s])
    """,
    re.VERBOSE,
)


def _extra_fn_set(extra_functions: Iterable[str] | None) -> frozenset[str]:
    return frozenset(str(n).strip().upper() for n in (extra_functions or []) if n)


def _validate_expr(
    expr: str, extra_functions: frozenset[str] | None = None,
) -> str | None:
    """Validate a SELECT expression against the whitelist.

    Returns an error message when the expression uses anything not in the
    whitelist; None when it is compile-safe.

    Allowed: whitelisted functions (aggregations, Doris-safe scalars, window
    functions, task-scoped extras), identifiers, numeric/string literals,
    boolean/keyword tokens (OVER/PARTITION/CASE/WHEN/AND/…), and a small
    operator set. Anything else (LATERAL VIEW, UNNEST, subqueries, unknown
    functions) is rejected.
    """
    extra = extra_functions or frozenset()
    if not expr.strip():
        return "empty expression"
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            return f"unrecognized token at: {expr[pos:pos+20]!r}"
        if m.lastgroup == "func":
            name = m.group("func").upper()
            if name in EXPR_KEYWORDS:
                pass  # `OVER (` / `PARTITION (` — keyword, not a function call
            elif name not in AGG_FUNCTIONS and name not in SCALAR_FUNCTIONS \
                    and name not in WINDOW_FUNCTIONS and name not in extra:
                return f"function {name}() not on the compile whitelist"
        # ident tokens: EXPR_KEYWORDS (OVER/PARTITION/CASE/…) or plain
        # column identifiers — both compile-safe; column existence is the
        # harness's job, not the compiler's.
        pos = m.end()
    # No LATERAL / UNNEST / subquery markers anywhere (belt & braces)
    upper = expr.upper()
    for banned in ("LATERAL", "UNNEST", "EXPLODE", "SELECT ", "FROM "):
        if banned in upper:
            return f"banned construct: {banned.strip()}"
    return None


# ── Compile ─────────────────────────────────────────────────────────


def _bind_column_set(
    expr: str,
    design: SemanticDesign,
    source_columns: set[str] | dict[str, set[str]],
) -> set[str] | None:
    """Columns to bind ``expr`` against.

    A ``set`` is the ADS single-source case. A ``dict`` is keyed by compiler
    alias (``a``/``b``) and table names so ``b.card_code`` is checked against
    the DIM, not a union that would steal ``dim_code`` onto alias ``a``
    (task 449dc3ca). ``None`` means the alias/table was not resolved — leave
    the expression unchanged.
    """
    if isinstance(source_columns, set):
        return source_columns
    m = _SIMPLE_SOURCE_EXPR_RE.fullmatch(expr)
    qual = (m.group("qual") or "").lower() if m else ""
    if not qual:
        qual = first_expr_alias(expr)
    if qual:
        if qual in source_columns:
            return source_columns[qual]
        return None
    if len(design.sources) == 1:
        from wren_agent.compiler.templates import _source_aliases

        src = design.sources[0]
        alias = _source_aliases(design.sources).get(src, "a")
        return (
            source_columns.get(alias)
            or source_columns.get(strip_source_alias(src).lower())
            or source_columns.get(strip_db_prefix(src).lower())
        )
    return None


def bind_source_exprs(
    design: SemanticDesign,
    source_columns: set[str] | dict[str, set[str]] | None,
) -> None:
    """Rewrite / drop columns so simple ``source_expr`` values exist on the source.

    Mutates ``design.columns`` in place. No-op when ``source_columns`` is
    empty (caller could not resolve the source schema). Period-family names
    (``stat_date`` vs ``stat_month``) and DIM ``code``/``name`` families
    remap when the bound source has exactly one matching column; invented
    passthrough attributes (``org_name``) are dropped. ADS output names are
    kept (``stat_month AS inventory_month``).
    """
    if not source_columns or not design.columns:
        return
    kept = []
    for col in design.columns:
        expr = (col.source_expr or "").strip() or col.name
        cols = _bind_column_set(expr, design, source_columns)
        if cols is None:
            kept.append(col)
            continue
        new_expr = remap_simple_source_expr(expr, cols)
        if new_expr is None:
            logger.info(
                "[compiler] dropping column `%s` — `%s` is not on the source",
                col.name, expr,
            )
            continue
        if new_expr != expr:
            old_m = _SIMPLE_SOURCE_EXPR_RE.fullmatch(expr)
            new_m = _SIMPLE_SOURCE_EXPR_RE.fullmatch(new_expr)
            col.source_expr = new_expr
            # Passthrough (no agg): bind the OUTPUT name to the physical
            # source column so leftover tables / harness see the same ident
            # (task f7055bbf: ADS `monthly_inventory_line_count` → DWS
            # `monthly_inventory_row_count`). Keep a distinct output name
            # when the LLM already aliased (`stat_month AS inventory_month`).
            if (
                old_m and new_m and not old_m.group("agg")
                and col.name.lower() == old_m.group("col").lower()
            ):
                col.name = new_m.group("col")
        kept.append(col)
    design.columns = kept


_JOIN_EQ_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
)

_JOIN_KEY_SKIP = frozenset({
    "is_current", "effective_date", "expiration_date",
    "create_time", "update_time", "version_date", "version_number",
})


def _joinable_column_names(cols: set[str]) -> set[str]:
    """Shared-key candidates: drop SCD / audit / display-name columns."""
    out: set[str] = set()
    for col in cols:
        name = col.lower()
        if name in _JOIN_KEY_SKIP or name.startswith("__ods"):
            continue
        if name.endswith("_name"):
            continue
        out.add(name)
    return out


def repair_both_missing_join_keys(
    design: SemanticDesign,
    source_columns: set[str] | dict[str, set[str]] | None,
) -> None:
    """Rewrite ``x.missing = y.missing`` to the unique shared joinable column.

    Task 5c814729: ``b.asset_type_id = c.asset_type_id`` is absent on both
    tables; the unique remaining join key is ``dim_code``. One-sided
    inventions stay refused (task 2efe882a: ``a.sku_id = b.dim_id``).
    No-op unless ``source_columns`` is a per-alias dict.
    """
    if not isinstance(source_columns, dict) or not design.joins:
        return
    for join in design.joins:
        on = str(join.get("on") or "")
        if not on:
            continue

        def _rewrite(match: re.Match[str]) -> str:
            left_alias, left_col, right_alias, right_col = match.groups()
            left_cols = _bind_column_set(
                f"{left_alias}.{left_col}", design, source_columns,
            )
            right_cols = _bind_column_set(
                f"{right_alias}.{right_col}", design, source_columns,
            )
            if not left_cols or not right_cols:
                return match.group(0)
            left_l = {c.lower() for c in left_cols}
            right_l = {c.lower() for c in right_cols}
            if left_col.lower() in left_l or right_col.lower() in right_l:
                return match.group(0)
            shared = _joinable_column_names(left_l) & _joinable_column_names(right_l)
            if len(shared) != 1:
                return match.group(0)
            key = next(iter(shared))
            return f"{left_alias}.{key} = {right_alias}.{key}"

        new_on = _JOIN_EQ_RE.sub(_rewrite, on)
        if new_on != on:
            logger.info(
                "[compiler] join ON both-sides missing — %s → %s",
                on, new_on,
            )
            join["on"] = new_on


_AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.I)
_GRAIN_EMPTY_CMP_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:(?P<alias>[A-Za-z_][\w$]*)\.)?(?P<col>[A-Za-z_][\w$]*)
      | (?:TRIM|LTRIM|RTRIM)\s*\(\s*(?:(?P<talias>[A-Za-z_][\w$]*)\.)?(?P<tcol>[A-Za-z_][\w$]*)\s*\)
    )
    \s*(?:!=|<>)\s*''
    \s*$
    """,
    re.I | re.VERBOSE,
)
_COALESCE_HEAD_RE = re.compile(r"\bCOALESCE\s*\(", re.I)
_DATE_EXPR_HEAD_RE = re.compile(r"^\s*(?:DATE_FORMAT|DATE_TRUNC|DATE)\s*\(", re.I)


def _string_grain_column_names(design: SemanticDesign) -> set[str]:
    """VARCHAR grain members that are not DATE_FORMAT/DATE expressions."""
    names = {c.name.lower() for c in design.columns if c.is_key}
    names.update(str(g).lower() for g in (design.grain or []))
    string_grain: set[str] = set()
    for col in design.columns:
        if col.name.lower() not in names:
            continue
        dtype = (col.dtype or "").upper()
        if dtype and not any(
            m in dtype for m in ("VARCHAR", "CHAR", "STRING", "TEXT")
        ):
            continue
        expr = (col.source_expr or "").strip()
        if _DATE_EXPR_HEAD_RE.match(expr):
            continue
        string_grain.add(col.name.lower())
    return string_grain


def _empty_cmp_grain_column(pred: str, string_grain: set[str]) -> str | None:
    m = _GRAIN_EMPTY_CMP_RE.match(pred or "")
    if not m:
        return None
    col = (m.group("col") or m.group("tcol") or "").lower()
    return col if col in string_grain else None


def rewrite_varchar_grain_empty_filters(design: SemanticDesign) -> None:
    """Drop ``grain_col != ''`` and bucket NULLs as ``未知``.

    Task e3914a7e: the skill's VARCHAR empty-string guard is for dirty
    date/number strings fed to DATE_FORMAT/CAST. Applied to a location grain
    it deleted every NULL and materialized an empty DWS. Non-grain
    ``dim_code != ''`` is left alone.
    """
    if getattr(design, "layer", "") != "DWS":
        return
    string_grain = _string_grain_column_names(design)
    if not string_grain:
        return
    hit: set[str] = set()

    def strip_preds(text: str) -> str:
        parts = _AND_SPLIT_RE.split(text) if text else []
        kept: list[str] = []
        for part in parts:
            col = _empty_cmp_grain_column(part.strip(), string_grain)
            if col:
                hit.add(col)
                continue
            if part.strip():
                kept.append(part.strip())
        return " AND ".join(kept)

    design.filters = [p for p in (strip_preds(f) for f in (design.filters or [])) if p]
    for join in design.joins or []:
        on = str(join.get("on") or "")
        if on:
            join["on"] = strip_preds(on)
    if not hit:
        return
    for col in design.columns:
        if col.name.lower() not in hit:
            continue
        expr = (col.source_expr or col.name).strip()
        if not expr or _COALESCE_HEAD_RE.search(expr):
            continue
        col.source_expr = f"COALESCE(NULLIF({expr}, ''), '未知')"
        logger.info(
            "[compiler] varchar grain `%s` empty-filter → COALESCE(..., '未知')",
            col.name,
        )


def _is_string_literal(expr: str) -> bool:
    s = (expr or "").strip()
    return len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]


def _split_top_level(expr: str, sep: str) -> list[str]:
    """Split ``expr`` on ``sep`` outside quotes and parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    depth = 0
    n = len(expr)
    sep_len = len(sep)
    while i < n:
        c = expr[i]
        if c in ("'", '"'):
            j = i + 1
            q = c
            while j < n:
                if expr[j] == q:
                    if q == "'" and j + 1 < n and expr[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(expr[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            buf.append(c)
            i += 1
            continue
        if depth == 0 and expr.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += sep_len
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _rewrite_top_level_pipes(s: str) -> str:
    parts = _split_top_level(s, "||")
    if len(parts) < 2:
        return s
    stripped = [p.strip() for p in parts]
    if any(_is_string_literal(p) for p in stripped):
        return "CONCAT(" + ", ".join(stripped) + ")"
    return s


def rewrite_pipe_concat(expr: str) -> str:
    """Rewrite Postgres-style concat ``||`` to Doris ``CONCAT``.

    Task 60a570e6: ``SUBSTRING(...) || '-Q' || CEIL(...)`` is parsed as
    boolean OR, then ``'-Q' can't cast to bool in strict mode``. Chains
    that include a string-literal operand become ``CONCAT(...)``. Boolean
    ``a = 1 || b = 1`` (no literal operand) is left alone.
    """
    if not expr or "||" not in expr:
        return expr

    def rec(s: str) -> str:
        out: list[str] = []
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c in ("'", '"'):
                j = i + 1
                q = c
                while j < n:
                    if s[j] == q:
                        if q == "'" and j + 1 < n and s[j + 1] == "'":
                            j += 2
                            continue
                        j += 1
                        break
                    j += 1
                out.append(s[i:j])
                i = j
                continue
            if c == "(":
                depth = 1
                j = i + 1
                while j < n and depth:
                    if s[j] in ("'", '"'):
                        q = s[j]
                        j += 1
                        while j < n:
                            if s[j] == q:
                                if q == "'" and j + 1 < n and s[j + 1] == "'":
                                    j += 2
                                    continue
                                j += 1
                                break
                            j += 1
                        continue
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                    j += 1
                inner = rec(s[i + 1:j - 1]) if j > i + 1 else ""
                out.append("(" + inner + ")")
                i = j
                continue
            out.append(c)
            i += 1
        return _rewrite_top_level_pipes("".join(out))

    return rec(expr)


_SELECT_AS_RE = re.compile(r"\s+AS\s+(`?[\w$]+`?)\s*$", re.I)


def rewrite_pipe_concat_sql(sql: str) -> str:
    """Rewrite string-literal ``||`` chains in an INSERT/SELECT statement."""
    if not sql or "||" not in sql:
        return sql
    m = re.search(r"\bSELECT\b", sql, re.I)
    if not m:
        return rewrite_pipe_concat(sql)
    rest = sql[m.end():]
    from_m = re.search(r"\bFROM\b", rest, re.I)
    if not from_m:
        return sql
    items = _split_top_level(rest[:from_m.start()], ",")
    new_items: list[str] = []
    for item in items:
        am = _SELECT_AS_RE.search(item)
        if am:
            new_items.append(
                rewrite_pipe_concat(item[:am.start()]) + item[am.start():]
            )
        else:
            new_items.append(rewrite_pipe_concat(item))
    return sql[:m.end()] + " " + ", ".join(new_items) + " " + rest[from_m.start():]


def rewrite_pipe_concat_design(design: SemanticDesign) -> None:
    for col in design.columns:
        expr = col.source_expr or ""
        new = rewrite_pipe_concat(expr)
        if new != expr:
            logger.info("[compiler] || concat → CONCAT on `%s`", col.name)
            col.source_expr = new
    design.filters = [rewrite_pipe_concat(f) for f in (design.filters or [])]
    for join in design.joins or []:
        on = str(join.get("on") or "")
        if on:
            join["on"] = rewrite_pipe_concat(on)


def _alias_to_source_table(design: SemanticDesign) -> dict[str, str]:
    """Compiler alias letter → `schema.table` (or bare) for error text."""
    from wren_agent.compiler.templates import _source_aliases

    alias_map = _source_aliases(design.sources)
    out: dict[str, str] = {}
    for src in design.sources:
        alias = alias_map.get(src)
        if alias:
            out[alias] = strip_source_alias(src)
    return out


def closed_world_problems(
    design: SemanticDesign,
    source_columns: set[str] | dict[str, set[str]] | None,
) -> list[str]:
    """JOIN ON / filter ``alias.column`` must exist on the explored snapshot.

    No unique-hit remap: ``b.dim_id`` is not rewritten to ``sku_id``
    (task 2efe882a). Returns problem strings with Available columns so the
    repair loop can pick a real identifier. No-op when ``source_columns`` is
    missing (unit tests / alias-letter rewrite without a snapshot).
    """
    if not source_columns:
        return []
    from wren_agent.compiler.rules import _qualified_column_idents
    from wren_agent.compiler.templates import (
        _infer_join_alias_subst,
        _source_aliases,
    )

    alias_map = _source_aliases(design.sources)
    subst = _infer_join_alias_subst(design, alias_map)
    alias_table = _alias_to_source_table(design)
    problems: list[str] = []

    def check_expr(expr: str, kind: str) -> None:
        for orig_alias, col in _qualified_column_idents(expr or ""):
            resolved = subst.get(orig_alias, orig_alias)
            cols = _bind_column_set(f"{resolved}.{col}", design, source_columns)
            if cols is None:
                continue
            if col.lower() in {c.lower() for c in cols}:
                continue
            table = alias_table.get(resolved, resolved)
            avail_txt = ", ".join(sorted(cols))
            problems.append(
                f"{kind} `{orig_alias}.{col}` is not in explored source "
                f"`{table}` (alias {resolved}). Available columns: {avail_txt}"
            )

    for join in design.joins or []:
        check_expr(str(join.get("on") or ""), "JOIN ON")
    for filt in design.filters or []:
        check_expr(filt or "", "filter")
    return problems


def try_compile_semantic_design(
    design: SemanticDesign,
    *,
    layer_landing_db: str = "",
    table_comment: str = "",
    source_columns: set[str] | dict[str, set[str]] | None = None,
    allowed_source_tables: set[str] | frozenset[str] | None = None,
    extra_functions: Iterable[str] | None = None,
) -> tuple[CompiledSQL | None, list[str]]:
    """Compile one semantic design; return ``(sql, problems)``.

    ``problems`` is non-empty when compile is refused (closed-world JOIN,
    goal source allowlist, off-whitelist expr, missing grain, …). Callers
    inject those strings into ``design_revise`` instead of emitting SQL the
    harness must reject. ``extra_functions`` are task-scoped scalars already
    admitted via EXPLAIN (ADR-0018); they are not written to the permanent
    whitelist.
    """
    from wren_agent.compiler.templates import apply_compiler_alias_contract

    apply_composed_table_name(design)
    apply_compiler_alias_contract(design)
    bind_source_exprs(design, source_columns)
    wrap_dws_non_aggregated(design)
    repair_both_missing_join_keys(design, source_columns)
    rewrite_varchar_grain_empty_filters(design)
    rewrite_pipe_concat_design(design)

    problems: list[str] = []

    if not design.sources:
        problems.append("no source tables declared")
    if not design.columns:
        problems.append("no columns declared")
    # DWS summary tables must be keyed by grain (UNIQUE KEY); ADS may omit
    # the grain (DUPLICATE model, RANDOM distribution — design-ads skill).
    if design.layer == "DWS" and not design.grain:
        problems.append("no grain/key columns declared")

    extra_fn = _extra_fn_set(extra_functions)
    # Every column needs an expression; keys may fall back to plain names.
    for col in design.columns:
        expr = col.source_expr.strip() or col.name
        err = _validate_expr(expr, extra_fn)
        if err:
            problems.append(f"column `{col.name}`: {err} ({expr!r})")

    # Multi-source designs MUST carry explicit joins or the aliased comma
    # form — an unaliased multi-source FROM is a harness failure class.
    if len(design.sources) > 1 and not design.joins:
        problems.append(
            "multi-source design must declare joins (alias contract a, b, …)"
        )
    if design.joins:
        known = {strip_db_prefix(s) for s in design.sources}
        known_full = {strip_source_alias(s) for s in design.sources} | set(design.sources)
        for j in design.joins:
            right_raw = str(j.get("right", ""))
            right = strip_db_prefix(right_raw)
            right_full = strip_source_alias(right_raw)
            if (right and right not in known
                    and right_full not in known_full
                    and right_raw not in design.sources):
                problems.append(
                    f"join references {j.get('right')} which is not in sources"
                )

    problems.extend(closed_world_problems(design, source_columns))
    problems.extend(source_allowlist_problems(design, allowed_source_tables))

    if problems:
        logger.warning(
            "[compiler] table %s NOT compiled: %s",
            design.summary_table, "; ".join(problems[:6]),
        )
        return None, problems

    ddl = render_ddl(
        design, landing_db=layer_landing_db, table_comment=table_comment,
    )
    load_sql = render_load_sql(design, landing_db=layer_landing_db)
    bare = normalize_table_name(design.summary_table, layer=design.layer)
    # Metadata form (no backticks) — must equal the plan design's
    # summary_table so the harness matches artifacts by target.
    return CompiledSQL(
        layer=design.layer,
        target_table=f"{layer_landing_db}.{bare}" if layer_landing_db else bare,
        ddl=ddl,
        load_sql=load_sql,
    ), []


def compile_semantic_design(
    design: SemanticDesign,
    *,
    layer_landing_db: str = "",
    table_comment: str = "",
    source_columns: set[str] | dict[str, set[str]] | None = None,
    allowed_source_tables: set[str] | frozenset[str] | None = None,
    extra_functions: Iterable[str] | None = None,
) -> CompiledSQL | None:
    """Compile one semantic design into DDL + load SQL.

    Returns None (with a logged reason) when the design cannot be compiled
    safely — the caller then keeps the LLM's own SQL for that table.

    ``source_columns`` (optional): physical names of the upstream table, or
    a dict keyed by compiler alias / table name for multi-source DWS JOINs.
    When provided, skill-example identifiers are remapped or dropped before
    rendering so the compiler does not emit SQL the harness must reject
    (tasks 9f42f542 / 449dc3ca). JOIN ON / filter identifiers are closed-
    world: a name absent from the explored snapshot refuses compile and is
    not unique-hit remapped (task 2efe882a: ``b.dim_id``). LLM join letters
    that collide with compiler ``a,b,c`` are rewritten (task 5c814729:
    ``c``/``t`` → ``b``/``c``). When **both** sides of an equality are
    missing, the unique shared joinable column is used. ``allowed_source_tables``
    is the goal closed set of bare names (task 94a11f97); extra JOIN tables
    refuse compile and are not remapped.
    """
    compiled, _problems = try_compile_semantic_design(
        design,
        layer_landing_db=layer_landing_db,
        table_comment=table_comment,
        source_columns=source_columns,
        allowed_source_tables=allowed_source_tables,
        extra_functions=extra_functions,
    )
    return compiled


def landing_db_for_layer(state: object, layer: str) -> str:
    """Resolve the layer's landing database from agent state (warehouse mode).

    Returns '' in business-db mode — DDL/DML stay bare and the deployment
    datasource picks the database.
    """
    if not state or state.get("source_mode") != "warehouse":  # type: ignore[union-attr]
        return ""
    try:
        from wren_agent.nodes.deployment import _warehouse_layers

        return landing_db_for(layer, _warehouse_layers(state))
    except Exception:
        return ""
