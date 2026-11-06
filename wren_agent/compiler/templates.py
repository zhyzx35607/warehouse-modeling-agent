"""DDL / DML template renderers for the DesignCompiler (ADR-0015).

Single source of truth for Doris SQL shape: these templates are unit-tested
against the exact grammar the real Doris cluster accepts (KEY-clause order,
COMMENT-before-DISTRIBUTED, HASH distribution on UNIQUE KEY tables,
replication_num PROPERTIES, explicit INSERT target column list). The LLM no
longer re-guesses this grammar in compiler mode.

The templates mirror the examples in design-dwd-dws.md / design-ads.md so a
compiled artifact is visually identical to a hand-written correct one.
"""

from __future__ import annotations

import re as _re

from wren_agent.compiler.rules import (
    map_doris_type,
    normalize_table_name,
    render_comment,
    strip_db_prefix,
    strip_source_alias,
)
from wren_agent.config import settings
from wren_agent.models.semantic import SemanticColumn, SemanticDesign


def _ddl_properties() -> str:
    return f"PROPERTIES('replication_num' = '{settings.doris.replication_num}')"


def _qualified_table(bare: str, landing_db: str) -> str:
    """DDL target: bare in business mode, `db`.`table` in warehouse mode."""
    if landing_db:
        return f"`{landing_db}`.`{bare}`"
    return bare


def _column_def(col: SemanticColumn) -> str:
    """`name TYPE COMMENT '中文注释'` — type normalized by the rules."""
    dtype = map_doris_type(col.dtype, is_key=col.is_key)
    return (
        f"`{col.name}` {dtype} "
        f"COMMENT '{render_comment(col.comment, col.name)}'"
    )


def render_ddl(
    design: SemanticDesign,
    *,
    landing_db: str = "",
    table_comment: str = "",
) -> str:
    """Render a CREATE TABLE statement for a semantic design.

    Clause order (Doris grammar): columns → [UNIQUE KEY(grain)] → COMMENT →
    DISTRIBUTED BY → PROPERTIES. The semantic model validator guarantees the
    grain is an ordered prefix of the column list, so `UNIQUE KEY` can never
    be misaligned. ADS tables may omit the grain — then no key clause is
    rendered (DUPLICATE model, RANDOM distribution, per the design-ads
    skill's optional-key rule).
    """
    bare = normalize_table_name(design.summary_table, layer=design.layer)
    grain = design.grain
    col_defs = ",\n  ".join(_column_def(c) for c in design.columns)
    comment = render_comment(table_comment or design.summary_name, bare)
    lines = [
        f"-- 粒度: ({', '.join(grain) if grain else design.layer + ' row'})",
        f"CREATE TABLE IF NOT EXISTS {_qualified_table(bare, landing_db)} (",
        f"  {col_defs}",
        ")",
    ]
    if grain:
        key_list = ", ".join(f"`{g}`" for g in grain)
        lines.append(f"UNIQUE KEY({key_list})")
    lines += [
        f"COMMENT '{comment}'",
    ]
    if grain:
        lines.append(f"DISTRIBUTED BY HASH(`{grain[0]}`) BUCKETS AUTO")
    else:
        lines.append("DISTRIBUTED BY RANDOM BUCKETS AUTO")
    lines.append(f"{_ddl_properties()};")
    return "\n".join(lines)


# ── DML (load SQL) rendering ────────────────────────────────────────


def _source_aliases(sources: list[str]) -> dict[str, str]:
    """table → alias letter (a, b, c… in source order). Multi-source load SQL
    MUST alias every source (skill rule, task 81df9f70); the LLM writes join
    conditions against these letters, so the contract is stable.

    Keys include the raw source string, the alias-stripped schema.table, and
    the bare table name so `joins.right` lookups work whether or not the LLM
    baked an alias into the name (task 971a8151).
    """
    lookup: dict[str, str] = {}
    for i, src in enumerate(sources):
        alias = chr(ord("a") + i)
        lookup[src] = alias
        lookup[strip_source_alias(src)] = alias
        lookup[strip_db_prefix(src)] = alias
    return lookup


def _resolve_join_right(right: str, alias_map: dict[str, str]) -> tuple[str, str]:
    """Bare table name + compiler alias for a join.right value."""
    raw = str(right or "")
    bare = strip_source_alias(raw)
    alias = (
        alias_map.get(raw)
        or alias_map.get(bare)
        or alias_map.get(strip_db_prefix(raw))
        or bare
    )
    return bare, alias


_ON_ALIAS_RE = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.")


def _aliases_in_expr(expr: str) -> list[str]:
    """Qualified aliases in appearance order (``d.col`` → ``d``)."""
    seen: list[str] = []
    for m in _ON_ALIAS_RE.finditer(expr or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _infer_join_alias_subst(
    design: SemanticDesign, alias_map: dict[str, str],
) -> dict[str, str]:
    """Map LLM join/SELECT letters (d, t) onto compiler a, b, c.

    FROM is rendered with source-order aliases; the LLM often writes ON and
    ``source_expr`` against a different alphabet (task 18594730:
    ``JOIN dim_sku b ON a.x = d.sku_id`` → Doris
    ``Unknown column 'sku_id' in 'd'``). This is the existing a,b,c
    contract, not explored-schema column binding.
    """
    subst: dict[str, str] = {}
    if design.sources:
        first = alias_map.get(design.sources[0], "a")
        subst[first] = first
    for j in design.joins or []:
        _bare, right_alias = _resolve_join_right(str(j.get("right", "")), alias_map)
        on = str(j.get("on") or "")
        on_aliases = _aliases_in_expr(on)
        # A letter already mapped to another table is NOT this join's right
        # (task 5c814729: LLM ``c`` = card collides with compiler ``c`` =
        # third source). Decide presence after applying subst.
        resolved_in_on = {subst.get(q, q) for q in on_aliases}
        if right_alias not in resolved_in_on:
            unbound = [q for q in on_aliases if q not in subst]
            if len(unbound) == 1:
                subst[unbound[0]] = right_alias
            elif unbound:
                subst[unbound[-1]] = right_alias
        # Identity for the compiler letter only when it is not already a
        # key mapping to a different table.
        if right_alias not in subst:
            subst[right_alias] = right_alias
    return {k: v for k, v in subst.items() if k != v}


def apply_compiler_alias_contract(design: SemanticDesign) -> None:
    """Rewrite JOIN ON / SELECT / filter letters onto compiler a, b, c.

    Mutates ``design`` in place so later bind/closed-world checks see the
    same aliases the FROM clause will render. No-op when the LLM already
    used source-order letters.
    """
    alias_map = _source_aliases(design.sources)
    subst = _infer_join_alias_subst(design, alias_map)
    if not subst:
        return
    for col in design.columns:
        if col.source_expr:
            col.source_expr = _apply_alias_subst(col.source_expr, subst)
    for join in design.joins or []:
        on = str(join.get("on") or "")
        if on:
            join["on"] = _apply_alias_subst(on, subst)
    design.filters = [
        _apply_alias_subst(f, subst) if f else f
        for f in (design.filters or [])
    ]


def _apply_alias_subst(sql: str, subst: dict[str, str]) -> str:
    """Replace ``old.col`` with ``new.col`` for each LLM→compiler alias."""
    if not sql or not subst:
        return sql
    pat = _re.compile(
        r"\b(" + "|".join(_re.escape(k) for k in sorted(subst, key=len, reverse=True))
        + r")\."
    )
    return pat.sub(lambda m: subst[m.group(1)] + ".", sql)


def _single_source_needs_alias(design: SemanticDesign) -> bool:
    """True when a single-source load SQL must alias its FROM table.

    The design LLM writes `a.cycle_start_date`, `a.org_id` … against the
    source — even for a single source (compiler-mode prompt shows aliases in
    the examples). Rendering `FROM tbl` bare then produces
    `SELECT a.col … FROM tbl … WHERE a.x` — an undefined alias Doris rejects
    (task 8e76b357: `mismatched input '('` on every DWS load SQL). When any
    column expression or filter references the source's alias letter, render
    `FROM tbl a` so the reference resolves.
    """
    if len(design.sources) != 1:
        return False
    alias = _source_aliases(design.sources).get(design.sources[0])
    if not alias:
        return False
    pat = _re.compile(rf"\b{_re.escape(alias)}\.\w+")
    for col in design.columns:
        if col.source_expr and pat.search(col.source_expr):
            return True
    return any(pat.search(f or "") for f in (design.filters or []))


def render_load_sql(
    design: SemanticDesign,
    *,
    landing_db: str = "",
) -> str:
    """Render the idempotent `INSERT INTO t (cols…) SELECT … GROUP BY` load.

    - Explicit target column list in DDL column order (harness contract:
      fields ⊆ DDL columns ⊆ INSERT target columns — true by construction).
    - Grain columns → GROUP BY, in grain order.
    - Every source table carries an alias when the SQL reads >1 source.
    - Filters AND-joined into WHERE.
    """
    bare = normalize_table_name(design.summary_table, layer=design.layer)
    target = f"`{landing_db}`.`{bare}`" if landing_db else bare
    col_names = [c.name for c in design.columns]

    alias_map = _source_aliases(design.sources)
    alias_subst = _infer_join_alias_subst(design, alias_map)
    multi = len(design.sources) > 1

    select_parts = []
    for col in design.columns:
        expr = col.source_expr.strip() if col.source_expr else col.name
        # ADS reads DWS summary tables — physical columns are already
        # aggregated at the DWS grain. An extra `MAX(x)`/`SUM(x)` shell on a
        # DWS physical column is redundant AND wrong without a GROUP BY (it
        # collapses the whole table to one row), so strip the shell down to
        # the bare column (task 11a576bb). Aggregate shells over a derivation
        # (e.g. `SUM(a-b)`) stay untouched — the LLM's semantic intent wins.
        if design.layer == "ADS":
            m = _re.fullmatch(r"\s*(?:MAX|SUM|AVG|MIN|COUNT)\s*\(\s*([A-Za-z_][\w$]*)\s*\)\s*", expr)
            if m:
                expr = m.group(1)
        expr = _apply_alias_subst(expr, alias_subst)
        # In multi-source SQL every column reference must carry its alias —
        # plain names would silently resolve to the first source.
        select_parts.append(f"       {expr} AS `{col.name}`")
    select_text = ",\n".join(select_parts)

    if design.joins:
        # chain: FROM src0 a JOIN src1 b ON <on> [JOIN src2 c ON <on> …]
        # Each join entry: {"right": "<source table>", "on": "<cond with aliases>"}
        # — the accumulated left side is already in FROM; aliases are a, b, c…
        # in source order, so the LLM writes `on` against those letters.
        # Source names are alias-stripped (task 971a8151: LLM-baked `tbl a`
        # must not render as `FROM tbl a a`).
        src0 = design.sources[0] if design.sources else ""
        parts = (
            [f"FROM {strip_source_alias(src0)} {alias_map[src0]}"] if src0 else []
        )
        for j in design.joins:
            right_bare, right_alias = _resolve_join_right(
                str(j.get("right", "")), alias_map,
            )
            on = _apply_alias_subst(str(j.get("on") or ""), alias_subst)
            parts.append(f"JOIN {right_bare} {right_alias} ON {on}")
        from_clause = " ".join(parts)
    else:
        if multi:
            srcs = ", ".join(
                f"{strip_source_alias(s)} {alias_map[s]}" for s in design.sources
            )
            from_clause = f"FROM {srcs}"
        elif design.sources and _single_source_needs_alias(design):
            src = design.sources[0]
            from_clause = f"FROM {strip_source_alias(src)} {alias_map[src]}"
        else:
            from_clause = (
                f"FROM {strip_source_alias(design.sources[0])}"
                if design.sources else ""
            )

    lines = [
        f"INSERT INTO {target} ({', '.join(f'`{n}`' for n in col_names)})",
        f"SELECT",
        select_text,
        from_clause,
    ]
    if design.filters:
        lines.append(
            "WHERE " + " AND ".join(
                _apply_alias_subst(f, alias_subst) for f in design.filters
            )
        )
    # GROUP BY only for DWS (it aggregates DWD/DIM detail rows). ADS reads the
    # DWS summary tables — one row per grain already — so its SELECT is plain
    # column passthrough + derived expressions; a GROUP BY there would demand
    # every physical column be aggregated and Doris rejects it with
    # "not in aggregate's output" (task 11a576bb: every ADS load SQL failed
    # at execution with exactly that error and the revision loop could not
    # fix it, because the GROUP BY was compiler-rendered, not a design flaw).
    if design.layer == "DWS" and design.grain:
        lines.append("GROUP BY " + ", ".join(f"`{g}`" for g in design.grain))
    lines[-1] = lines[-1] + ";"
    return "\n".join(lines)