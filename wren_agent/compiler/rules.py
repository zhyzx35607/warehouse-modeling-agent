"""Environment rules for the DesignCompiler (ADR-0015).

Every Doris/environment constraint that used to be "guessed" by the LLM each
task (and patched by the validator afterwards) lives here as deterministic
rules. The compiler applies them at render time; the templates never produce
SQL that violates them.

Sources of the rules: design-dwd-dws.md / design-ads.md skills, the
validator's patch helpers (validation.py `_shorten_plan_table_names`,
`_fix_plan_target_db_prefixes`, `_normalize_ddl_properties`) and the
extra_context hard rules in design_layers.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from wren_agent.config import settings

# ── Aggregation functions valid in DWS/ADS SELECT expressions ───────

AGG_FUNCTIONS: frozenset[str] = frozenset({"SUM", "COUNT", "MAX", "MIN", "AVG"})

# Window functions accepted inside OVER(...) clauses (real goals need
# last-value-in-day patterns, e.g. daily closing balance via
# MAX(IF(ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1, col, NULL)) —
# observed in real-task acceptance: the single DWS design was rejected
# because ROW_NUMBER was off-whitelist).
WINDOW_FUNCTIONS: frozenset[str] = frozenset({
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD",
})

# Keywords allowed inside expressions (OVER/PARTITION clauses, CASE/WHEN,
# boolean operators) — the tokenizer accepts them as plain tokens.
EXPR_KEYWORDS: frozenset[str] = frozenset({
    "OVER", "PARTITION", "BY", "ORDER", "ASC", "DESC", "AND", "OR", "NOT",
    "IN", "IS", "NULL", "BETWEEN", "LIKE", "CASE", "WHEN", "THEN", "ELSE",
    "END", "DISTINCT", "TRUE", "FALSE",
})

# Doris-safe scalar functions the compiler's expression whitelist allows.
# Kept deliberately small — anything exotic should be reviewed semantically,
# not rendered blindly.
SCALAR_FUNCTIONS: frozenset[str] = frozenset({
    "DATE", "DATE_FORMAT", "DATE_TRUNC", "SUBSTRING", "SUBSTR",
    "COALESCE", "IF", "NULLIF", "CONCAT", "QUARTER", "YEAR", "MONTH",
    "DAY", "HOUR", "CAST", "ABS", "ROUND", "FLOOR", "CEIL", "IFNULL",
    "TRIM", "LTRIM", "RTRIM",
})

# ── Type normalization (Doris dialect) ──────────────────────────────

# Doris forbids STRING/BOOLEAN in KEY columns; the compiler maps them before
# rendering instead of leaving it to the LLM to remember.
TYPE_ALIASES: dict[str, str] = {
    "STRING": "VARCHAR(65533)",
    "BOOLEAN": "TINYINT",
    "BOOL": "TINYINT",
    "INT": "INT",
    "INTEGER": "INT",
    "BIGINT": "BIGINT",
    "SMALLINT": "SMALLINT",
    "TINYINT": "TINYINT",
    "DECIMAL": "DECIMAL(20,2)",
    "NUMERIC": "DECIMAL(20,2)",
    "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "VARCHAR": "VARCHAR(255)",
    "CHAR": "CHAR(20)",
    "TEXT": "VARCHAR(65533)",
    "JSON": "VARCHAR(65533)",
    "BLOB": "VARCHAR(65533)",
}


def map_doris_type(dtype: str, *, is_key: bool = False) -> str:
    """Normalize a declared type to a Doris-valid type.

    STRING/BOOLEAN in KEY positions are the two failure classes the LLM kept
    repeating (extra_context hard rules, tasks 63f401d/1fe0acc) — mapping them
    here makes the violation impossible in compiled SQL.
    """
    t = (dtype or "").strip().upper()
    if not t:
        return "VARCHAR(255)"
    # Keep explicit sizes: DECIMAL(18,2), VARCHAR(64), CHAR(8)...
    if re.fullmatch(r"[A-Z]+\(\d+(\.\d+)?(,\s*\d+)?\)", t):
        base = t.split("(")[0]
        if base in ("VARCHAR", "CHAR", "DECIMAL", "DECIMALV3"):
            return t
        if base == "STRING":
            return "VARCHAR(65533)"  # STRING(x) isn't a Doris signature
        return t
    mapped = TYPE_ALIASES.get(t)
    if mapped:
        return mapped
    return t


def is_key_safe_type(dtype: str) -> bool:
    """KEY columns must be orderable, non-SYSTEM types (skill rule: keys use
    VARCHAR(255) / BIGINT / INT / DATE — never STRING)."""
    t = map_doris_type(dtype, is_key=True)
    return not t.startswith("VARCHAR(65533)") or "STRING" in dtype.upper()


# ── Table-name rules ────────────────────────────────────────────────
# Mirrors validation.py `_shorten_plan_table_names`: keep layer prefix and
# grain suffix, drop the longest underscore segment until ≤ limit.

_DORIS_TABLE_NAME_LIMIT: int = 64


def table_name_limit() -> int:
    return getattr(settings.doris, "table_name_length_limit", _DORIS_TABLE_NAME_LIMIT) or _DORIS_TABLE_NAME_LIMIT


# Table refs here are `schema.table` or `table` — they never contain spaces.
# A trailing identifier (`dwd_x a`, `dwd_x AS t`) is always an alias the
# design LLM baked into `sources` / `joins.right`.
_TRAILING_SOURCE_ALIAS_RE = re.compile(
    r"^(?P<table>(?:`?[\w$-]+`?\.)?`?[\w$-]+`?)"
    r"\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*)\s*$",
    re.I,
)


def strip_source_alias(name: str) -> str:
    """Strip a trailing SQL table alias from a source / join.right name.

    Compiler-mode prompts say 'declare joins with aliases a, b, c…' and the
    warehouse hard rules show `FROM schema.table t`. The LLM therefore often
    returns `sources: ["dwd_x a", "dim_y b"]`. Rendering those as
    `FROM dwd_x a a` is rejected by Doris (`mismatched input 'a'`,
    task 971a8151). Keep the schema.table; drop only the alias token.
    """
    s = (name or "").strip()
    m = _TRAILING_SOURCE_ALIAS_RE.match(s)
    return m.group("table") if m else s


# Period-grain synonyms the design LLM copies from skill examples. When the
# source has exactly one column from this family, rewrite the identifier to
# that column (task 9f42f542: ADS `source_expr: "stat_date"` against a DWS
# table whose period column is `stat_month`).
PERIOD_COLUMN_NAMES: frozenset[str] = frozenset({
    "stat_date", "stat_month", "stat_quarter", "stat_year", "stat_dt",
    "inventory_month", "inventory_date", "inventory_dt",
    "biz_date", "biz_month", "biz_dt",
    "period_date", "period_month", "dt", "month",
})

# Core-model DIM skill renames source `code`/`name` → `dim_code`/`dim_name`
# (comments keep 编号/名称). Warehouse DWS then guesses `card_code`/`card_name`
# from the table name (task 449dc3ca). Same unique-hit remap as the period
# family — only when that DIM table has exactly one member of the family.
DIM_CODE_NAMES: frozenset[str] = frozenset({"code", "dim_code", "card_code"})
DIM_NAME_NAMES: frozenset[str] = frozenset({"name", "dim_name", "card_name"})

# ADS copies a DWS count measure with a sibling suffix (task f7055bbf:
# `monthly_inventory_line_count` vs DWS `monthly_inventory_row_count`).
_COUNT_SUFFIXES: tuple[str, ...] = (
    "_line_count", "_row_count", "_record_count", "_cnt",
)

_SIMPLE_SOURCE_EXPR_RE = re.compile(
    r"^\s*(?:(?P<agg>SUM|COUNT|MAX|MIN|AVG)\s*\(\s*)?"
    r"(?:(?P<qual>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<col>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*\))?\s*$",
    re.I,
)

# First alias.column in a complex expr (task 891fc962: COALESCE(b.dim_code)).
_QUALIFIED_COL_RE = re.compile(
    r"(?<![A-Za-z0-9_`])([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
)

_AGG_HEAD_RE = re.compile(r"^\s*(SUM|COUNT|MAX|MIN|AVG)\s*\(", re.I)


def remap_simple_source_expr(expr: str, source_columns: set[str]) -> str | None:
    """Bind a simple SELECT expression to ``source_columns``.

    Returns the (possibly rewritten) expression, or ``None`` to drop the
    column. Complex expressions (arithmetic, CAST, DATE()) are returned
    unchanged — the harness still checks them. Simple passthrough /
    ``MAX(col)`` of an unknown identifier is dropped rather than inventing a
    JOIN (task 9f42f542: skill-example ``org_name`` was not on the DWS).
    DIM ``code``/``name`` families remap the same way (``card_code`` →
    ``dim_code`` when that is the unique hit on the bound alias). Count
    measures remap ``*_line_count`` / ``*_row_count`` the same way
    (task f7055bbf).
    """
    if source_columns is None:
        return expr
    avail = {c.lower(): c for c in source_columns}
    m = _SIMPLE_SOURCE_EXPR_RE.fullmatch(expr)
    if not m:
        # Complex expr (COALESCE, CAST, DATE_FORMAT). Keep only when every
        # qualified column exists on the bound source; otherwise drop
        # (task 891fc962: ADS `MAX(COALESCE(b.dim_name))` as org_name).
        for _alias, ident in _qualified_column_idents(expr):
            if ident not in avail:
                return None
        return expr
    ident = m.group("col")
    low = ident.lower()
    mapped: str | None = None
    if low in avail:
        mapped = avail[low]
    elif low in PERIOD_COLUMN_NAMES:
        hits = [avail[c] for c in avail if c in PERIOD_COLUMN_NAMES]
        if len(hits) == 1:
            mapped = hits[0]
    elif low in DIM_CODE_NAMES:
        hits = [avail[c] for c in avail if c in DIM_CODE_NAMES]
        if len(hits) == 1:
            mapped = hits[0]
    elif low in DIM_NAME_NAMES:
        hits = [avail[c] for c in avail if c in DIM_NAME_NAMES]
        if len(hits) == 1:
            mapped = hits[0]
    else:
        count_hits = _count_family_hits(low, avail)
        if len(count_hits) == 1:
            mapped = count_hits[0]
    if mapped is None:
        return None
    if mapped.lower() == low:
        return expr
    qual = m.group("qual")
    agg = m.group("agg")
    inner = f"{qual}.{mapped}" if qual else mapped
    return f"{agg}({inner})" if agg else inner


def _qualified_column_idents(expr: str) -> list[tuple[str, str]]:
    """``alias.column`` pairs in ``expr``, lowercased."""
    return [
        (m.group(1).lower(), m.group(2).lower())
        for m in _QUALIFIED_COL_RE.finditer(expr)
    ]


def first_expr_alias(expr: str) -> str:
    """Compiler alias of the first ``alias.column`` in a (possibly complex) expr."""
    m = _SIMPLE_SOURCE_EXPR_RE.fullmatch(expr)
    if m and m.group("qual"):
        return m.group("qual").lower()
    pairs = _qualified_column_idents(expr)
    return pairs[0][0] if pairs else ""


def wrap_dws_non_aggregated(design) -> None:
    """Wrap non-grain DWS SELECT exprs so Doris GROUP BY is legal.

    Task 891fc962: ``COALESCE(b.dim_code, '未知')`` at ``org_id``+month was
    rejected with ``dim_code, dim_name not in aggregate's output``. Grain
    keys and already-aggregated measures are left alone; everything else
    gets ``MAX(...)``.
    """
    if getattr(design, "layer", "") != "DWS":
        return
    grain = {str(g).lower() for g in (getattr(design, "grain", None) or [])}
    if not grain:
        return
    for col in design.columns or []:
        name = str(getattr(col, "name", "") or "").lower()
        if getattr(col, "is_key", False) or name in grain:
            continue
        expr = (getattr(col, "source_expr", None) or "").strip()
        if not expr or _AGG_HEAD_RE.match(expr):
            continue
        col.source_expr = f"MAX({expr})"


def _count_family_hits(low: str, avail: dict[str, str]) -> list[str]:
    """Unique ``*_line_count`` / ``*_row_count`` sibling on ``avail``."""
    stem = None
    for suf in _COUNT_SUFFIXES:
        if low.endswith(suf) and len(low) > len(suf):
            stem = low[: -len(suf)]
            break
    if stem is None:
        return []
    hits: list[str] = []
    for suf in _COUNT_SUFFIXES:
        cand = stem + suf
        if cand in avail:
            hits.append(avail[cand])
    return hits


def strip_db_prefix(name: str) -> str:
    """`db_name.table` → bare `table` (backticked forms included).

    Trailing SQL aliases are stripped first so `db.dwd_x a` yields `dwd_x`,
    not `dwd_x a`.
    """
    return strip_source_alias(name).split(".")[-1].strip("`")


def normalize_table_name(name: str, *, layer: str = "") -> str:
    """Normalize a table name: bare, backtick-stripped, ≤ limit.

    Names that exceed the limit are shortened the same way the validator used
    to patch them (drop longest underscore segment; keep prefix + suffix).
    """
    bare = strip_db_prefix(name).strip().strip("`").lower()
    limit = table_name_limit()
    if len(bare) <= limit:
        return bare
    segments = bare.split("_")
    while len(bare) > limit and len(segments) > 2:
        # drop the longest middle segment (keep first=layer prefix, last=grain)
        longest = max(range(1, len(segments) - 1), key=lambda i: len(segments[i]))
        segments.pop(longest)
        bare = "_".join(segments)
    return bare[:limit]


# ── Grain-aware table names ─────────────────────────────────────────
# Distinct grains of the same topic used to collapse to ``dws_{topic}_{period}``
# (card×location vs org monthly both became ``dws_stock_inventory_monthly``).
# Encode short entity tokens before the period suffix; never exceed the Doris
# identifier limit — drop topic fillers first, keep layer / grain / period.

_LAYER_PREFIXES: frozenset[str] = frozenset({"dws", "ads", "dwd", "dim", "ods", "dwm"})
_TOPIC_FILLERS: frozenset[str] = frozenset({
    "info", "detail", "details", "data", "fact", "facts", "base",
    "tbl", "table", "src", "tmp", "temp", "extended", "snapshot",
})
_PERIOD_SEGMENTS: frozenset[str] = frozenset({
    "month", "monthly", "quarter", "quarterly", "year", "yearly",
    "day", "daily", "date",
})
_NOT_PERIOD_RE = re.compile(r"(life|warranty|expire|valid|shelf|_age)", re.I)
_ENTITY_EXACT: dict[str, str] = {
    "sku_id": "card",
    "sku": "card",
    "card_id": "card",
    "storage_location": "loc",
    "location": "loc",
    "org_id": "org",
    "org_code": "org",
    "organization_id": "org",
    "manage_org_id": "org",
    "company_id": "company",
    "company_code": "company",
    "building_id": "bldg",
    "bldg_id": "bldg",
    "store_id": "store",
    "room_id": "product",
    "basics_room_id": "product",
    "unit_id": "unit",
    "dept_id": "dept",
    "department_id": "dept",
    "tenant_id": "tenant",
    "customer_id": "cust",
    "contract_id": "contract",
    "project_id": "proj",
    "user_id": "user",
}
_ENTITY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("sku_id", "card"),
    ("storage_location", "loc"),
    ("_store_id", "store"),
    ("_room_id", "product"),
    ("_building_id", "bldg"),
    ("_company_id", "company"),
    ("_org_id", "org"),
    ("_card_id", "card"),
    ("_unit_id", "unit"),
    ("_dept_id", "dept"),
    ("_tenant_id", "tenant"),
    ("_contract_id", "contract"),
)


def _period_token_from_col(col: str) -> str:
    """Map a grain column to ``daily`` / ``monthly`` / … or empty."""
    low = (col or "").strip().lower()
    if not low or _NOT_PERIOD_RE.search(low):
        return ""
    if low in PERIOD_COLUMN_NAMES:
        if "quarter" in low:
            return "quarterly"
        if "year" in low:
            return "yearly"
        if "month" in low:
            return "monthly"
        return "daily"
    for suffix, token in (
        ("_month", "monthly"),
        ("month", "monthly"),
        ("_quarter", "quarterly"),
        ("quarter", "quarterly"),
        ("_year", "yearly"),
        ("_date", "daily"),
        ("_dt", "daily"),
        ("_day", "daily"),
    ):
        if low == suffix.lstrip("_") or low.endswith(suffix):
            return token
    return ""


def _period_token_from_grain(grain: list[str] | tuple[str, ...] | None) -> str:
    for col in grain or []:
        token = _period_token_from_col(str(col))
        if token:
            return token
    return ""


def _entity_token_from_col(col: str) -> str:
    low = (col or "").strip().lower()
    if not low or _period_token_from_col(low):
        return ""
    if low in _ENTITY_EXACT:
        return _ENTITY_EXACT[low]
    for suffix, token in _ENTITY_SUFFIXES:
        if low == suffix.lstrip("_") or low.endswith(suffix):
            return token
    if low.endswith("_id") and len(low) > 3:
        stem = low[:-3]
        last = stem.split("_")[-1]
        if last and last not in _TOPIC_FILLERS and last not in {"id", "dim", "fk", "pk"}:
            return last[:8]
    return ""


def _entity_tokens_from_grain(grain: list[str] | tuple[str, ...] | None) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for col in grain or []:
        token = _entity_token_from_col(str(col))
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _purpose_token(summary_name: str) -> str:
    """ADS application token. ``看板`` alone is not ``quality`` (dashboards)."""
    text = summary_name or ""
    low = text.lower()
    if "质量" in text or "quality" in low:
        return "quality"
    if "明细" in text or "detail" in low:
        return "detail"
    if any(k in text for k in ("排名", "排行")) or "rank" in low:
        return "rank"
    return ""


def _topic_from_sources(sources: list[str] | None) -> list[str]:
    for src in sources or []:
        bare = strip_db_prefix(src).lower()
        rest = ""
        for prefix in ("dwd_", "fct_", "fact_", "dws_", "ads_"):
            if bare.startswith(prefix):
                rest = bare[len(prefix):]
                break
        if not rest:
            continue
        segs = [
            s for s in rest.split("_")
            if s and s not in _TOPIC_FILLERS and s not in _PERIOD_SEGMENTS
        ]
        if segs:
            return segs
    return []


def _dedupe_segments(segments: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in segments:
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _fit_composed_name(segments: list[str], protected: set[str]) -> str:
    """Keep the name ≤ Doris limit without dropping grain / layer / period."""
    segs = _dedupe_segments(segments)
    limit = table_name_limit()
    bare = "_".join(segs)

    def _drop(candidates: list[int]) -> bool:
        nonlocal segs, bare
        if not candidates:
            return False
        i = max(candidates, key=lambda idx: len(segs[idx]))
        segs.pop(i)
        bare = "_".join(segs)
        return True

    while len(bare) > limit and len(segs) > 2:
        filler_idx = [
            i for i, s in enumerate(segs)
            if i > 0 and s in _TOPIC_FILLERS and s not in protected
        ]
        if _drop(filler_idx):
            continue
        topic_idx = [
            i for i, s in enumerate(segs)
            if i > 0 and s not in protected
        ]
        if not _drop(topic_idx):
            break
    if len(bare) > limit:
        return normalize_table_name(bare)
    return bare


def compose_layer_table_name(
    layer: str,
    grain: list[str] | tuple[str, ...] | None,
    *,
    sources: list[str] | None = None,
    current: str = "",
    summary_name: str = "",
) -> str:
    """Build a ≤64-char ``{layer}_{topic}_{entities}_{period}`` table name.

    Existing names that already contain every grain entity token and the
    period suffix are kept (then fitted to the length limit). Otherwise the
    missing tokens are spliced in before the period — so
    ``dws_stock_inventory_monthly`` at card×location grain becomes
    ``dws_stock_inventory_card_loc_monthly``, not a colliding org-monthly name.
    """
    layer_p = (layer or "dws").strip().lower()
    if layer_p not in ("dws", "ads"):
        return normalize_table_name(current or "", layer=layer_p)

    entities = _entity_tokens_from_grain(grain)
    period = _period_token_from_grain(grain)
    purpose = _purpose_token(summary_name) if layer_p == "ads" else ""
    # ADS purpose already distinguishes the application; skip repeating period.
    ads_period = "" if purpose else period
    dws_period = period if layer_p == "dws" else ads_period
    protected = {layer_p, *entities}
    if dws_period:
        protected.add(dws_period)
    if purpose:
        protected.add(purpose)

    current_bare = strip_db_prefix(current or "").strip().strip("`").lower()
    segs = [s for s in current_bare.split("_") if s]
    have = set(segs)
    missing_ent = [t for t in entities if t not in have]
    need_period = bool(dws_period) and dws_period not in have
    need_purpose = bool(purpose) and purpose not in have

    if segs and segs[0] in _LAYER_PREFIXES:
        segs[0] = layer_p
        # ADS names that already look specific (topic + period/purpose) are
        # kept — only generic ``ads_{topic}`` gets grain/purpose spliced.
        if layer_p == "ads" and len(segs) >= 4:
            if need_purpose:
                segs.append(purpose)
            return _fit_composed_name(segs, protected | set(segs))
        if not missing_ent and not need_period and not need_purpose:
            return _fit_composed_name(segs, protected)
        body = segs[1:]
        if purpose and body and body[-1] == purpose:
            body = body[:-1]
        if dws_period and body and body[-1] == dws_period:
            body = body[:-1]
        body = [
            s for s in body
            if s not in _PERIOD_SEGMENTS and s != purpose
        ]
        for token in entities:
            if token not in body:
                body.append(token)
        new_segs = [layer_p] + body
        if dws_period:
            new_segs.append(dws_period)
        if purpose:
            new_segs.append(purpose)
        return _fit_composed_name(new_segs, protected)

    topic = [
        s for s in _topic_from_sources(sources)
        if s not in entities and s != purpose
    ]
    new_segs = [layer_p] + topic + entities
    if dws_period:
        new_segs.append(dws_period)
    if purpose:
        new_segs.append(purpose)
    return _fit_composed_name(new_segs, protected)


def uniquify_table_name(
    bare: str,
    occupied: Iterable[str] | None = None,
    *,
    layer: str = "",
) -> str:
    """Return ``bare`` or ``{bare}_vN`` so the name is not in ``occupied``.

    Used when the landing database already has the composed name (leftover
    from an earlier run). Suffix stays within the Doris identifier limit by
    dropping the longest middle segment, then truncating.
    """
    occupied_l = {
        strip_db_prefix(str(n)).lower()
        for n in (occupied or [])
        if n
    }
    candidate = normalize_table_name(bare, layer=layer)
    if candidate not in occupied_l:
        return candidate
    limit = table_name_limit()
    for n in range(2, 50):
        suffix = f"_v{n}"
        budget = max(1, limit - len(suffix))
        stem = candidate
        segs = stem.split("_")
        while len(stem) > budget and len(segs) > 2:
            longest = max(range(1, len(segs) - 1), key=lambda i: len(segs[i]))
            segs.pop(longest)
            stem = "_".join(segs)
        if len(stem) > budget:
            stem = stem[:budget].rstrip("_") or stem[:budget]
        name = f"{stem}{suffix}"
        if len(name) > limit:
            name = name[:limit]
        if name not in occupied_l:
            return name
    return (candidate[: max(1, limit - 3)] + "_x")[:limit]


def apply_composed_table_name(design, occupied: Iterable[str] | None = None) -> str:
    """Write the grain-aware bare name onto ``design.summary_table``."""
    grain = list(getattr(design, "grain", None) or [])
    if not grain:
        grain = [
            str(getattr(c, "name", "") or "")
            for c in (getattr(design, "columns", None) or [])
            if getattr(c, "is_key", False)
        ]
    bare = compose_layer_table_name(
        layer=str(getattr(design, "layer", "") or "DWS"),
        grain=grain,
        sources=list(getattr(design, "sources", None) or []),
        current=str(getattr(design, "summary_table", "") or ""),
        summary_name=str(getattr(design, "summary_name", "") or ""),
    )
    if occupied:
        bare = uniquify_table_name(
            bare, occupied, layer=str(getattr(design, "layer", "") or ""),
        )
    design.summary_table = bare
    return bare


def landing_db_for(layer: str, layers: dict[str, str]) -> str:
    """Layer → landing database (warehouse mode). Empty = no prefix."""
    return (layers or {}).get(layer.lower()) or ""


# ── Chinese comments ────────────────────────────────────────────────
# Extracted from design_layers._zh_comment so the compiler owns the fallback
# comment derivation without importing the design nodes (no cycle).

_ZHUYIN_SUFFIXES: dict[str, str] = {
    "id": "ID", "name": "名称", "code": "编码", "type": "类型", "status": "状态",
    "date": "日期", "time": "时间", "amount": "金额", "count": "数量",
    "cnt": "数量", "num": "数量", "qty": "数量", "total": "合计", "amt": "金额",
    "balance": "余额", "rate": "比率", "ratio": "比率", "desc": "描述",
    "remark": "备注", "comment": "备注", "flag": "标志", "no": "编号",
    "user": "用户", "create": "创建", "update": "更新", "modify": "修改",
    "month": "月份", "year": "年份", "day": "日期", "hour": "小时",
    "start": "开始", "end": "结束", "stat": "统计", "daily": "每日",
    "monthly": "每月", "yearly": "每年", "quarterly": "每季度",
    "unpaid": "未收", "paid": "已收", "receivable": "应收", "received": "实收",
    "contract": "合同", "product": "商品", "shop": "店铺", "unit": "单位",
    "org": "组织", "company": "公司", "dept": "部门", "tenant": "客户",
    "order": "订单", "fee": "费用", "income": "收入", "expense": "支出",
    "flow": "流水", "amount_receivable": "应收金额",
    "amount_paid": "实收金额", "paid_amount": "实收金额",
}


def default_zh_comment(col_name: str) -> str:
    """Derive a Chinese comment from a column name (fallback when the LLM
    did not supply one — every column/table must carry a Chinese comment)."""
    name = col_name.strip().lstrip("_")
    if not name:
        return col_name
    # whole-name dictionary hits first
    if name in _ZHUYIN_SUFFIXES:
        return _ZHUYIN_SUFFIXES[name]
    parts = [p for p in name.split("_") if p]
    if not parts:
        return col_name
    # daily_xxx / monthly_xxx prefixes map to 每日/每月…
    zh = []
    for p in parts:
        if p in _ZHUYIN_SUFFIXES:
            zh.append(_ZHUYIN_SUFFIXES[p])
        else:
            zh.append(p)
    return "_".join(zh)


def render_comment(comment: str, fallback: str) -> str:
    """Escape single quotes and guarantee a non-empty Chinese comment."""
    c = (comment or "").strip() or default_zh_comment(fallback)
    return c.replace("'", "''")
