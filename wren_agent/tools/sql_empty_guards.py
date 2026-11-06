"""Strip empty-string guards from native numeric / date columns.

Doris INSERT with enable_insert_strict cannot cast '' to decimal. Comparing a
bigint/int/decimal/date column to '' (or wrapping it in NULLIF(col, '')) throws
`can't cast to decimal in strict mode` (task 0af225c9). Empty-string filters
belong only on VARCHAR/STRING/CHAR/TEXT — those can actually hold ''
(task 55f47744 parse-number-fail).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_STRING_MARKERS = ("VARCHAR", "CHAR", "STRING", "TEXT")
_NUMERIC_TEMPORAL_RE = re.compile(
    r"DECIMAL|DATETIME|TIMESTAMP|\b(?:BIGINT|LARGEINT|SMALLINT|TINYINT|"
    r"INTEGER|INT|DOUBLE|FLOAT|BOOLEAN|DATE)\b",
    re.I,
)

_TABLE_REF_RE = re.compile(
    r"(?:FROM|JOIN)\s+"
    r"(?:`?(?P<db>[\w$]+)`?\.)?`?(?P<table>[\w$]+)`?"
    r"(?:\s+(?:AS\s+)?`?(?P<alias>[A-Za-z_]\w*)`?)?",
    re.I,
)

_RESERVED_ALIAS = frozenset({
    "where", "join", "on", "group", "order", "limit", "union", "having",
    "select", "left", "right", "inner", "full", "cross", "set", "and", "or",
    "by", "into", "from", "as",
})

_NULLIF_CAST_RE = re.compile(
    r"NULLIF\s*\(\s*CAST\s*\(\s*(?P<col>(?:`?[\w$]+`?\.)?`?[\w$]+`?)\s+"
    r"AS\s+(?:STRING|VARCHAR)(?:\s*\([^)]*\))?\s*\)\s*,\s*''\s*\)",
    re.I,
)
_CAST_NULLIF_RE = re.compile(
    r"CAST\s*\(\s*NULLIF\s*\(\s*(?P<col>(?:`?[\w$]+`?\.)?`?[\w$]+`?)\s*,\s*''\s*\)"
    r"\s+AS\s+\w+(?:\s*\([^)]*\))?\s*\)",
    re.I,
)
_NULLIF_COL_RE = re.compile(
    r"NULLIF\s*\(\s*(?P<col>(?:`?[\w$]+`?\.)?`?[\w$]+`?)\s*,\s*''\s*\)",
    re.I,
)
_EMPTY_CMP_RE = re.compile(
    r"(?:CAST\s*\(\s*(?P<castcol>(?:`?[\w$]+`?\.)?`?[\w$]+`?)\s+"
    r"AS\s+(?:STRING|VARCHAR)(?:\s*\([^)]*\))?\s*\)"
    r"|(?P<col>(?:`?[\w$]+`?\.)?`?[\w$]+`?))"
    r"\s*(?:!=|<>)\s*''",
    re.I,
)
_WHERE_RE = re.compile(
    r"\bWHERE\b(?P<body>.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|;|$)",
    re.I | re.S,
)


@dataclass
class ColumnTypeIndex:
    by_table_col: dict[tuple[str, str], str] = field(default_factory=dict)
    by_col: dict[str, str] = field(default_factory=dict)


def is_string_type(dtype: str) -> bool:
    u = (dtype or "").upper()
    return any(m in u for m in _STRING_MARKERS)


def is_native_numeric_or_temporal(dtype: str) -> bool:
    if not dtype or is_string_type(dtype):
        return False
    return bool(_NUMERIC_TEMPORAL_RE.search(dtype))


def column_types_from_explorations(explorations: Any) -> ColumnTypeIndex:
    """Index explored column types as (table, col) and unambiguous bare names."""
    index = ColumnTypeIndex()
    col_types: dict[str, set[str]] = {}
    items = explorations.values() if isinstance(explorations, dict) else (explorations or [])
    for exp in items:
        for table in getattr(exp, "tables", None) or []:
            tname = str(getattr(table, "name", "") or "").lower()
            if not tname:
                continue
            for f in getattr(table, "fields", None) or []:
                col = str(getattr(f, "name", "") or "").lower()
                dtype = str(getattr(f, "data_type", "") or "")
                if not col:
                    continue
                index.by_table_col[(tname, col)] = dtype
                col_types.setdefault(col, set()).add(dtype)
    for col, dtypes in col_types.items():
        kinds = {is_native_numeric_or_temporal(d) for d in dtypes}
        if len(kinds) == 1:
            index.by_col[col] = next(iter(dtypes))
    return index


def _table_aliases(sql: str) -> tuple[list[str], dict[str, str]]:
    tables: list[str] = []
    alias_map: dict[str, str] = {}
    for m in _TABLE_REF_RE.finditer(sql or ""):
        table = (m.group("table") or "").lower()
        alias = (m.group("alias") or "").lower()
        if not table:
            continue
        tables.append(table)
        alias_map[table] = table
        if alias and alias not in _RESERVED_ALIAS:
            alias_map[alias] = table
    return tables, alias_map


def _resolve_dtype(
    index: ColumnTypeIndex, col_ref: str, tables: list[str], alias_map: dict[str, str],
) -> str | None:
    cleaned = col_ref.replace("`", "")
    parts = cleaned.split(".")
    col = parts[-1].lower()
    prefix = parts[-2].lower() if len(parts) >= 2 else ""
    if prefix:
        table = alias_map.get(prefix, prefix)
        dtype = index.by_table_col.get((table, col))
        if dtype:
            return dtype
    for table in tables:
        dtype = index.by_table_col.get((table, col))
        if dtype:
            return dtype
    return index.by_col.get(col)


def _is_native_ref(
    index: ColumnTypeIndex, col_ref: str, tables: list[str], alias_map: dict[str, str],
) -> bool:
    dtype = _resolve_dtype(index, col_ref, tables, alias_map)
    return bool(dtype) and is_native_numeric_or_temporal(dtype)


def _split_top_and(clause: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    text = clause or ""
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if (
            depth == 0
            and text[i:i + 3].upper() == "AND"
            and (i == 0 or not text[i - 1].isalnum())
            and (i + 3 >= len(text) or not text[i + 3].isalnum())
        ):
            parts.append("".join(buf).strip())
            buf = []
            i += 3
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _is_empty_cmp_on_native(
    pred: str, index: ColumnTypeIndex, tables: list[str], alias_map: dict[str, str],
) -> bool:
    m = _EMPTY_CMP_RE.fullmatch(pred.strip())
    if not m:
        return False
    ref = m.group("castcol") or m.group("col") or ""
    return _is_native_ref(index, ref, tables, alias_map)


def _rewrite_where(
    sql: str, index: ColumnTypeIndex, tables: list[str], alias_map: dict[str, str],
) -> str:
    m = _WHERE_RE.search(sql)
    if not m:
        return sql
    body = m.group("body")
    preds = _split_top_and(body)
    kept = [
        p for p in preds
        if not _is_empty_cmp_on_native(p, index, tables, alias_map)
    ]
    if kept == preds:
        return sql
    if not kept:
        return sql[:m.start()] + sql[m.end():]
    return sql[:m.start()] + "WHERE " + " AND ".join(kept) + sql[m.end():]


def _rewrite_select_nullifs(
    sql: str, index: ColumnTypeIndex, tables: list[str], alias_map: dict[str, str],
) -> str:
    def unwrap(match: re.Match, group: str = "col") -> str:
        ref = match.group(group)
        if _is_native_ref(index, ref, tables, alias_map):
            return ref
        return match.group(0)

    sql = _CAST_NULLIF_RE.sub(lambda m: unwrap(m), sql)
    sql = _NULLIF_CAST_RE.sub(lambda m: unwrap(m), sql)
    sql = _NULLIF_COL_RE.sub(lambda m: unwrap(m), sql)
    return sql


def rewrite_typed_empty_string_guards(sql: str, index: ColumnTypeIndex) -> str:
    """Drop `col != ''` / NULLIF(col,'') on native numeric/date columns only."""
    if not sql or not (index.by_table_col or index.by_col):
        return sql
    tables, alias_map = _table_aliases(sql)
    out = _rewrite_select_nullifs(sql, index, tables, alias_map)
    out = _rewrite_where(out, index, tables, alias_map)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def sanitize_sql_artifacts(
    artifacts: list[Any],
    explorations: Any,
    *,
    layer: str | None = None,
) -> int:
    """Rewrite DML artifacts in place. Returns how many statements changed."""
    index = column_types_from_explorations(explorations)
    changed = 0
    for art in artifacts or []:
        if layer and getattr(art, "layer", "") != layer:
            continue
        sql_type = str(getattr(art, "sql_type", "") or "")
        if not sql_type.startswith("DML"):
            continue
        original = getattr(art, "sql", "") or ""
        rewritten = rewrite_typed_empty_string_guards(original, index)
        from wren_agent.compiler.render import rewrite_pipe_concat_sql
        rewritten = rewrite_pipe_concat_sql(rewritten)
        if rewritten != original:
            art.sql = rewritten
            changed += 1
    return changed
