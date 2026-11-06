"""Regression grader for design-dwd-dws against the 21-round failure corpus.

Each input snapshot carries a modeling goal, the explored source columns, the
required grain combinations, and an `expectations` list naming which harness
rules this case must satisfy (each maps to one historical failure round):

  - explicit_column_list  (a23e1599): every DML INSERT has an explicit column list
  - no_invented_columns   (cc841bec): every alias.column in load_sql exists in
                           the explored source columns (or is a derived alias)
  - grain_coverage        (d7418ecc): every required grain combo is covered by
                           ≥1 DWS table's grain
  - no_derived_in_dws     (513c12a5): DWS fields must not declare derived ratio
                           names (rate/均价/占比/率)
  - doris_dialect         (e21ec05e): no two-arg TO_DATE, no yyyy-MM patterns
  - data_guard            (55f47744): date expressions guarded with NULLIF/WHERE
  - no_cast_text_key      (55f47744): no unguarded CAST(alias.col AS BIGINT)
                           on join keys (comma-joined / empty TEXT values
                           crash with parse number fail)
  - qualified_sources     (3dacf0ae): ADS source_dws_tables carry a known db prefix

Score = passed expectations / total expectations.
"""
import re
import sys
sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/..")
from common import load_params, report

output, inp = load_params()
errors = []
expectations = inp.get("expectations", [])
source_columns = {
    t.get("name", "").lower(): {
        f.get("name", "").lower() for f in (t.get("fields") or [])
    }
    for t in (inp.get("source_tables") or [])
}
required_grains = inp.get("required_grains", [])


def dws_designs():
    return output.get("dws_designs", [])


def dws_artifacts():
    return output.get("sql_artifacts", output.get("artifacts", []))


def check_explicit_column_list():
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        if not load_sql:
            continue
        # explicit `INSERT INTO t (cols)` — or, failing that, every SELECT
        # alias present (the parser-level fallback)
        m = re.search(r"INSERT\s+INTO\s+\w+\s*\(([^)]*)\)", load_sql, re.I)
        if not m:
            errors.append(
                f"{d.get('summary_table', '?')}: load_sql missing explicit "
                "INSERT target column list"
            )


def check_no_invented_columns():
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        if not load_sql:
            continue
        for match in re.finditer(r"([a-z_]\w*)\.([a-z_]\w*)", load_sql, re.I):
            alias, col = match.group(1).lower(), match.group(2).lower()
            # resolve alias to a source table via FROM/JOIN
            src = None
            for tm in re.finditer(
                r"(?:FROM|JOIN)\s+(?:[\w]+\.)?([\w]+)\s+(?:AS\s+)?([a-z_]\w*)", load_sql, re.I
            ):
                if tm.group(2).lower() == alias:
                    src = tm.group(1).lower()
            if src is None:
                continue
            if col in ("as", "on", "where", "group", "order", "by", "having",
                       "and", "or", "not", "null", "case", "when", "then",
                       "end", "else", "distinct", "cast", "sum", "count",
                       "avg", "min", "max", "coalesce", "nullif", "if",
                       "date_format", "substring", "concat", "year", "month",
                       "quarter", "day", "to_date", "current_date", "now"):
                continue
            cols = source_columns.get(src)
            if cols is not None and col not in cols:
                errors.append(
                    f"{d.get('summary_table', '?')}: invented column "
                    f"{alias}.{col} (not in source {src})"
                )


def check_grain_coverage():
    if not required_grains:
        return
    covered = set()
    for d in dws_designs():
        grain = [str(g) for g in d.get("grain", [])]
        fields = {str(f.get("name", "")).lower() for f in (d.get("fields") or [])}
        cols = set(grain) | fields
        for combo in required_grains:
            parts = combo.split("×")
            if all(
                any(p in c or c in p for c in cols) for p in parts
            ):
                covered.add(combo)
    for combo in required_grains:
        if combo not in covered:
            errors.append(f"required grain combo not covered: {combo}")


def check_no_derived_in_dws():
    derived_hint = re.compile(r"(rate|ratio|均价|占比|率|_pct$|_rate$|_ratio$)", re.I)
    for d in dws_designs():
        declared = [
            str(f.get("name", "")) for f in (d.get("fields") or [])
        ] + [
            str(m.get("name", "")) for m in (d.get("measures") or [])
            if isinstance(m, dict)
        ]
        for name in declared:
            if derived_hint.search(name):
                errors.append(
                    f"{d.get('summary_table', '?')}: DWS declares '{name}' "
                    "— derived ratio metrics belong to ADS; DWS materializes "
                    "base aggregations only"
                )


def _design_ddl_columns() -> dict[str, set[str]]:
    """summary_table(bare) -> declared column names from fields + DDL."""
    cols: dict[str, set[str]] = {}
    for d in dws_designs():
        table = str(d.get("summary_table", "")).rsplit(".", 1)[-1].lower()
        names = {
            str(f.get("name", "")).lower()
            for f in (d.get("fields") or [])
            if f.get("name")
        }
        names.update(
            str(m.get("name", "")).lower()
            for m in (d.get("measures") or []) if isinstance(m, dict)
        )
        names.update(
            str(g).lower() for g in (d.get("grain") or [])
        )
        cols[table] = names
    # also from sql_artifacts DDLs
    for a in (output.get("sql_artifacts") or []):
        if str(a.get("sql_type", "")).upper() != "DDL":
            continue
        table = str(a.get("target_table", "")).rsplit(".", 1)[-1].lower()
        ddl = str(a.get("sql", ""))
        m = re.search(r"CREATE\s+TABLE[^(]*\(([^)]*)\)", ddl, re.S | re.I)
        if m:
            cols.setdefault(table, set()).update(
                line.strip().rstrip(",").split()[0].strip("`").lower()
                for line in m.group(1).splitlines()
                if line.strip() and re.match(r"`?\w+`?\s", line.strip())
            )
    return cols


def check_self_consistent_columns():
    """Regression cc841bec/42452474: a DML's alias.column must exist in the
    design's own DDL for the referenced table — the invented-column defect
    occurred between plan-internal tables (org table referencing a column
    the shop-product table never defined)."""
    ddl_cols = _design_ddl_columns()
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        if not load_sql:
            continue
        # alias -> bare source table
        alias_src = {}
        for tm in re.finditer(
            r"(?:FROM|JOIN)\s+(?:[\w-]+\.)?([\w-]+)(?:\s+(?:AS\s+)?([a-z_]\w*))?",
            load_sql, re.I,
        ):
            tbl = tm.group(1).lower()
            alias = (tm.group(2) or "").lower() or tbl
            alias_src[alias] = tbl
        for m in re.finditer(r"([a-z_]\w*)\.([a-z_]\w*)", load_sql, re.I):
            alias, col = m.group(1).lower(), m.group(2).lower()
            src = alias_src.get(alias)
            if src is None:
                continue
            cols = ddl_cols.get(src)
            if cols is not None and col not in cols:
                errors.append(
                    f"{d.get('summary_table', '?')}: references "
                    f"{alias}.{col} not defined in {src}'s DDL"
                )


def check_doris_dialect():
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        if re.search(r"TO_DATE\s*\([^,)]*,\s*['\"]", load_sql, re.I):
            errors.append(
                f"{d.get('summary_table', '?')}: two-arg TO_DATE — Doris only "
                "accepts TO_DATE(expr)"
            )
        if re.search(r"\|\|\s*'|'\s*\|\|", load_sql):
            errors.append(
                f"{d.get('summary_table', '?')}: '||' is Doris boolean OR, "
                "not concat — use CONCAT(...) (task 60a570e6: "
                "'-Q' can't cast to bool in strict mode)"
            )


def check_data_guard():
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        if re.search(r"\b(?:year|month|quarter|day|date_format|to_date)\s*\(", load_sql, re.I):
            if not re.search(r"NULLIF\s*\(|!= ['\"]|<> ['\"]|IS NOT NULL", load_sql, re.I):
                errors.append(
                    f"{d.get('summary_table', '?')}: date/numeric expressions "
                    "unguarded — for VARCHAR date/numeric-as-text columns add "
                    "NULLIF(col,'') / WHERE col != ''; native DATE columns "
                    "may use IS NOT NULL (parse number fail on empty strings). "
                    "Do not use != '' on a VARCHAR grain — COALESCE(NULLIF(col,''),'未知')"
                )


def check_no_cast_text_key():
    """Join keys must not CAST a TEXT column to BIGINT without a NULLIF guard.

    A TEXT join key may hold comma-joined multi-values or empty strings;
    CAST('id1,id2' AS BIGINT) / CAST('' AS BIGINT) crash at execution with
    `parse number fail` (task 55f47744 — contract→store/product keys).
    """
    for d in dws_designs():
        load_sql = d.get("load_sql", "")
        for m in re.finditer(
            r"CAST\s*\(\s*([a-z_][a-z0-9_]*)\.([a-z0-9_]+)\s+AS\s+"
            r"(?:BIGINT|INT|DOUBLE|DECIMAL|FLOAT)",
            load_sql, re.I,
        ):
            # Guarded if the expression itself is wrapped in NULLIF(...)
            segment = load_sql[max(0, m.start() - 60):m.end()]
            if "NULLIF" in segment.upper():
                continue
            errors.append(
                f"{d.get('summary_table', '?')}: unguarded "
                f"CAST({m.group(1)}.{m.group(2)} AS ...) on a possibly "
                "multi-value/empty TEXT join key — NULLIF/explode it first "
                "(parse number fail, task 55f47744)"
            )


def check_qualified_sources():
    for d in dws_designs():
        for src in (d.get("source_tables") or []):
            if isinstance(src, str) and "." not in src:
                errors.append(
                    f"{d.get('summary_table', '?')}: unqualified source "
                    f"table {src}"
                )


CHECKS = {
    "explicit_column_list": check_explicit_column_list,
    "no_invented_columns": check_no_invented_columns,
    "self_consistent_columns": check_self_consistent_columns,
    "grain_coverage": check_grain_coverage,
    "no_derived_in_dws": check_no_derived_in_dws,
    "doris_dialect": check_doris_dialect,
    "data_guard": check_data_guard,
    "no_cast_text_key": check_no_cast_text_key,
    "qualified_sources": check_qualified_sources,
}

results: dict[str, list[str]] = {}
for expectation in expectations:
    fn = CHECKS.get(expectation)
    if not fn:
        continue
    before = len(errors)
    fn()
    results[expectation] = errors[before:]

for expectation, errs in results.items():
    for e in errs:
        sys.stderr.write(f"[{expectation}] {e}\n")

# score = expectations that produced no errors / total expectations
passed = sum(1 for errs in results.values() if not errs)
score = passed / max(len(results), 1)
report(score)
