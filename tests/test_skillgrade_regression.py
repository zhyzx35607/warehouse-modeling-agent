"""Tests for the skillgrade regression grader (21-round failure corpus).

The grader must detect each historical defect family in a DWS design and
pass a clean design without false positives.
"""

from __future__ import annotations

import json
import subprocess
import sys

GRADER = "eval/graders/check_design_dwd_dws_regression.py"
CASE03 = "eval/input/input_design_dwd_dws_regression_03.json"
CASE04 = "eval/input/input_design_dwd_dws_regression_04.json"


def _run_grader(out: dict, inp: str) -> float:
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False)
        r = subprocess.run(
            [sys.executable, GRADER, path, inp],
            capture_output=True, text=True, encoding="utf-8",
        )
        return float(r.stdout.strip() or 0)
    finally:
        os.remove(path)


def _base() -> dict:
    return {
        "dws_designs": [{
            "summary_table": "dws_rent_daily",
            "grain": ["stat_date", "org_id"],
            "group_by_dimensions": ["stat_date", "org_id"],
            "fields": [{"name": "stat_date"}, {"name": "org_id"},
                       {"name": "daily_rent", "comment": "日租金"}],
            "measures": [{"name": "daily_rent", "expression": "SUM(payment)"}],
            "load_sql": (
                "INSERT INTO dws_rent_daily (stat_date, org_id, daily_rent) "
                "SELECT stat_date, org_id, SUM(payment) FROM dwd_rent "
                "WHERE stat_date IS NOT NULL GROUP BY stat_date, org_id"
            ),
            "requirement_coverage": [{
                "requirement": "单位×日", "status": "supported",
                "evidence": ["stat_date", "org_id"], "gap_reason": "",
            }],
        }],
        "sql_artifacts": [{
            "layer": "DWS", "target_table": "dws_rent_daily", "sql_type": "DDL",
            "sql": ("CREATE TABLE dws_rent_daily (stat_date DATE, org_id VARCHAR(255), "
                    "daily_rent DECIMAL(20,2)) UNIQUE KEY(stat_date, org_id)"),
        }, {
            "layer": "DWS", "target_table": "dws_rent_daily", "sql_type": "DML_INSERT",
            "sql": (
                "INSERT INTO dws_rent_daily (stat_date, org_id, daily_rent) "
                "SELECT stat_date, org_id, SUM(payment) FROM dwd_rent "
                "WHERE stat_date IS NOT NULL GROUP BY stat_date, org_id"
            ),
        }],
    }


def test_grader_rejects_two_arg_to_date():
    d = _base()
    d["dws_designs"][0]["load_sql"] = (
        "INSERT INTO dws_rent_daily (stat_date, org_id, daily_rent) "
        "SELECT TO_DATE(stat_date, 'yyyy-MM'), org_id, SUM(payment) "
        "FROM dwd_rent GROUP BY stat_date, org_id"
    )
    assert _run_grader(d, CASE03) < 1.0


def test_grader_rejects_invented_column():
    d = _base()
    d["dws_designs"][0]["load_sql"] = (
        "INSERT INTO dws_rent_daily (stat_date, org_id, daily_rent) "
        "SELECT t.stat_date, t.org_id, SUM(t.quarterly_unpaid_fee) "
        "FROM dwd_rent t GROUP BY t.stat_date, t.org_id"
    )
    assert _run_grader(d, CASE03) < 1.0


def test_grader_rejects_derived_ratio_in_dws():
    d = _base()
    d["dws_designs"][0]["measures"] = [
        {"name": "daily_collection_rate", "expression": "paid / receivable"}
    ]
    assert _run_grader(d, CASE03) < 1.0


def test_grader_rejects_missing_explicit_column_list():
    d = _base()
    d["dws_designs"][0]["load_sql"] = (
        "INSERT INTO dws_rent_daily SELECT stat_date, org_id, SUM(payment) "
        "FROM dwd_rent WHERE stat_date IS NOT NULL GROUP BY stat_date, org_id"
    )
    assert _run_grader(d, CASE03) < 1.0


def test_grader_rejects_unguarded_date_expression():
    d = _base()
    d["dws_designs"][0]["load_sql"] = (
        "INSERT INTO dws_rent_daily (stat_date, org_id, daily_rent) "
        "SELECT YEAR(payment_period_date), org_id, SUM(payment) "
        "FROM dwd_rent GROUP BY payment_period_date, org_id"
    )
    assert _run_grader(d, CASE03) < 1.0


def test_grader_passes_clean_design():
    assert _run_grader(_base(), CASE04) == 1.0
