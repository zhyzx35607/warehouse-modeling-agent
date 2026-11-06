"""Common grading utilities for skill evaluation.

All graders receive: $OUTPUT (LLM output file) $INPUT (test input file).
They must print a single float: 0.0 (fail) to 1.0 (perfect).

Usage:
  python grader.py <output.json> <input.json>
  → prints: 0.85
"""

import json
import re
import sys


def load_params():
    """Parse grader args and load input/output."""
    if len(sys.argv) < 3:
        print("0.0")  # Missing args
        sys.exit(0)

    output_file = sys.argv[1]
    input_file = sys.argv[2]

    try:
        with open(output_file, encoding="utf-8") as f:
            output = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("0.0")
        sys.exit(0)

    try:
        with open(input_file, encoding="utf-8") as f:
            test_input = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        test_input = {}

    return output, test_input


def report(score: float):
    """Print score and exit."""
    print(str(round(score, 2)))
    sys.exit(0)


def check_field(output: dict, field: str, required_type=None) -> tuple[bool, str]:
    """Check that a field exists and has the correct type.

    Returns (present, detail).
    """
    if field not in output:
        return False, f"missing field: {field}"
    if required_type is not None:
        val = output[field]
        if required_type == "non_empty_string":
            if not isinstance(val, str) or not val.strip():
                return False, f"field '{field}' is not a non-empty string"
        elif required_type == "non_empty_list":
            if not isinstance(val, list) or len(val) == 0:
                return False, f"field '{field}' is not a non-empty list"
        elif required_type == "list_of_dicts":
            if not isinstance(val, list) or not all(isinstance(v, dict) for v in val):
                return False, f"field '{field}' is not a list of dicts"
        elif not isinstance(val, required_type):
            return False, f"field '{field}' type mismatch: expected {required_type}"
    return True, "ok"


_CJK = re.compile(r"[一-鿿]")


def _column_block(ddl: str) -> tuple[str, int]:
    """Extract the CREATE TABLE column block (text + index just past its `)`).

    Uses paren-depth counting so `DECIMAL(20,2) COMMENT '...'` and quoted
    Chinese text like `'同步方式(全量/增量)'` cannot confuse the parser;
    single-quoted strings and `--` line comments are skipped.
    """
    depth = None  # None until the first unquoted `(` opens the column block
    in_str = False
    start = 0
    i, n = 0, len(ddl)
    while i < n:
        ch = ddl[i]
        nxt = ddl[i + 1] if i + 1 < n else ""
        if not in_str and ch == "-" and nxt == "-":
            # Skip -- line comment (e.g. `-- 粒度: 每行代表 (id)`)
            j = ddl.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            if depth is None:
                depth = 1
                start = i + 1
            else:
                depth += 1
        elif ch == ")":
            if depth is not None:
                depth -= 1
                if depth == 0:
                    return ddl[start:i], i + 1
        i += 1
    return "", 0


def chinese_comment_errors(ddl: str) -> list[str]:
    """Check a DDL statement against the Chinese-comment hard rule (CONTEXT.md):
    column-level COMMENT clauses inside the column block, a table-level COMMENT
    after it, and any `-- 粒度`/`-- GRAIN` header comment must all be Chinese.

    Returns a list of human-readable errors (empty list = compliant).
    """
    if not ddl:
        return []
    errors = []

    body, tail_start = _column_block(ddl)
    col_comments = re.findall(r"COMMENT\s+'([^']*)'", body, re.I)
    if not col_comments:
        errors.append("no column-level COMMENT found")
    for val in col_comments:
        if not _CJK.search(val):
            errors.append(f"non-Chinese column COMMENT: '{val[:30]}'")

    table_comments = re.findall(r"COMMENT\s+'([^']*)'", ddl[tail_start:], re.I)
    if not table_comments:
        errors.append("no table-level COMMENT found")
    for val in table_comments:
        if not _CJK.search(val):
            errors.append(f"non-Chinese table COMMENT: '{val[:30]}'")

    for hm in re.finditer(r"--[^\n]*", ddl):
        header = hm.group(0)
        if "粒度" in header or "GRAIN" in header:
            if not _CJK.search(header):
                errors.append(f"non-Chinese header comment: {header[:40]}")

    return errors
