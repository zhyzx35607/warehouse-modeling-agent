"""Grade design-dwd-dws: verify DWD has grain, DWS has aggregation + valid Doris DDL + Chinese comments."""
import re
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, chinese_comment_errors

output, inp = load_params()
errors = []

# Check DWD
dwds = output.get("dwd_designs", output.get("facts", output.get("fact_tables", [])))
for d in dwds[:5]:
    for f in ["fact_name", "fact_table", "grain"]:
        if f not in d:
            errors.append(f"missing {f} in DWD design")
    grain = d.get("grain", [])
    if isinstance(grain, list) and len(grain) == 0:
        errors.append(f"empty grain in {d.get('fact_table','?')}")
    for e in chinese_comment_errors(d.get("ddl", "")):
        errors.append(f"{d.get('fact_table','?')}: {e}")
    load_sql = d.get("load_sql", "")
    if not re.search(r"\bINSERT\s+INTO\b", str(load_sql), re.I):
        errors.append(f"{d.get('fact_table','?')}: missing executable INSERT load_sql")


def check_dws_ddl(d):
    """Doris rules the DWS DDL must satisfy (see design-dwd-dws.md)."""
    ddl = d.get("ddl", "")
    if not ddl:
        return
    table = d.get("summary_table", "?")
    for e in chinese_comment_errors(ddl):
        errors.append(f"{table}: {e}")
    if re.search(r"AGGREGATE\s+KEY", ddl, re.I):
        errors.append(f"{table}: uses AGGREGATE KEY — DWS must use UNIQUE KEY")
        return
    m = re.search(r"UNIQUE\s+KEY\s*\(([^)]*)\)", ddl, re.I)
    if not m:
        errors.append(f"{table}: DDL missing UNIQUE KEY clause")
        return
    key_cols = [c.strip().strip("`") for c in m.group(1).split(",") if c.strip()]
    if not re.search(r"DISTRIBUTED\s+BY\s+HASH", ddl, re.I):
        errors.append(f"{table}: UNIQUE KEY table missing DISTRIBUTED BY HASH")
    # Doris grammar: table-level COMMENT must precede DISTRIBUTED BY
    dist = re.search(r"DISTRIBUTED\s+BY\b[^;]*", ddl, re.I)
    if dist and re.match(r"\s*COMMENT\s+'[^']*'", ddl[dist.end():], re.I | re.S):
        errors.append(
            f"{table}: table-level COMMENT placed after DISTRIBUTED BY — "
            f"Doris rejects it (mismatched input 'COMMENT')"
        )
    # Column definition order: first N defined columns must equal the key list
    body = re.search(r"\(\s*(.*)\)\s*UNIQUE\s+KEY", ddl, re.S | re.I)
    if body:
        defined = []
        for line in body.group(1).splitlines():
            line = line.strip().rstrip(",")
            mm = re.match(r"`?(\w+)`?\s+\w", line)
            if mm:
                defined.append(mm.group(1))
        if defined[: len(key_cols)] != key_cols:
            errors.append(
                f"{table}: UNIQUE KEY {key_cols} is not an ordered prefix "
                f"of defined columns {defined[:len(key_cols)]}"
            )


def check_dws_materialization(d):
    table = d.get("summary_table", "?")
    load_sql = d.get("load_sql", "")
    if not re.search(r"\bINSERT\s+INTO\b", load_sql, re.I):
        errors.append(f"{table}: missing executable INSERT load_sql")
    grain = [str(g) for g in d.get("grain", [])]
    if table.lower().endswith("_daily") and not any(
        re.search(r"(?:date|day|time|_dt$)", g, re.I) for g in grain
    ):
        errors.append(f"{table}: daily table grain has no date/time field")
    if d.get("group_by_dimensions", []) != d.get("grain", []):
        errors.append(f"{table}: group_by_dimensions must exactly match grain")

    coverage = d.get("requirement_coverage", [])
    if not coverage:
        errors.append(f"{table}: missing requirement_coverage")
    for item in coverage:
        if item.get("status") != "supported":
            errors.append(
                f"{table}: requirement is not fully supported: {item.get('requirement','?')}"
            )
        if not item.get("evidence"):
            errors.append(f"{table}: supported requirement has no evidence")

    declared = {
        str(field.get("name", "")).lower()
        for field in d.get("fields", []) if isinstance(field, dict)
    } | {
        str(measure.get("field_name") or measure.get("name") or "").lower()
        for measure in d.get("measures", []) if isinstance(measure, dict)
    } | {str(column).lower() for column in grain}
    ddl = d.get("ddl", "").lower()
    load_sql_lower = load_sql.lower()
    for column in declared - {""}:
        if not re.search(rf"\b{re.escape(column)}\b", ddl):
            errors.append(f"{table}: declared column {column} missing from DDL")
        if not re.search(rf"\b{re.escape(column)}\b", load_sql_lower):
            errors.append(f"{table}: declared column {column} missing from load_sql")


# Check DWS
dwss = output.get("dws_designs", output.get("summaries", output.get("summary_tables", [])))
for d in dwss[:5]:
    for f in ["summary_name", "summary_table", "source_dwd_table", "grain"]:
        if f not in d:
            errors.append(f"missing {f} in DWS design")
    check_dws_ddl(d)
    check_dws_materialization(d)

required = inp.get("required_requirements", [])
coverage_text = " ".join(
    str(item.get("requirement", ""))
    for design in dwss
    for item in design.get("requirement_coverage", [])
).lower()
for requirement in required:
    if str(requirement).lower() not in coverage_text:
        errors.append(f"requested requirement missing from coverage: {requirement}")

score = max(0, 1.0 - len(errors) * 0.2)
report(score)
