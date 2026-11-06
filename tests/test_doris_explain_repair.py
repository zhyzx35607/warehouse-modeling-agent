"""Regression tests: Doris EXPLAIN probe %-format bug + cross-layer repair.

Two fixes:
1. `DorisClient.query` no longer forwards an empty params tuple to pymysql
   (`cur.execute(sql, ())` makes mogrify treat every `%` in the SQL as a
   printf placeholder — `DATE_FORMAT(x, '%Y-%m')` failed with `not enough
   arguments for format string` on perfectly valid statements).
2. `design_revise` remaps cross-layer failures (DORIS_EXPLAIN /
   doris_dml_insert_columns — layers that have no SQLArtifact of their own)
   onto the layer that owns the failing table, so the repair loop actually
   targets the artifacts instead of short-circuiting into a 0.4ms no-op that
   the no-progress guard then fails the task on (task 3d19c031).
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from wren_agent.models.design import ADSDesign, DWDesignPlan, DWSDesign, SQLArtifact
from wren_agent.models.task import ValidationLevel, ValidationResult
from wren_agent.state import create_initial_state


# ── Fix 1: DorisClient.query empty-params handling ─────────────


class _FakeCursor:
    def __init__(self, calls: list):
        self.calls = calls
        self.description = None  # query path: SELECT returns no columns

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, *args):
        self.calls.append(args)


class _FakeConn:
    def __init__(self, calls: list):
        self._calls = calls

    def cursor(self):
        return _FakeCursor(self._calls)

    def close(self):
        pass


def _patch_pymysql(monkeypatch, calls: list):
    mod = types.ModuleType("pymysql")
    mod.connect = lambda **kwargs: _FakeConn(calls)
    monkeypatch.setitem(sys.modules, "pymysql", mod)


@pytest.mark.asyncio
async def test_query_no_params_executes_raw_sql(monkeypatch):
    """No params → cur.execute(sql) — `%Y-%m` must NOT hit pymysql %-formatting."""
    from wren_agent.tools.doris_client import DorisClient

    calls: list = []
    _patch_pymysql(monkeypatch, calls)

    sql = "SELECT DATE_FORMAT(cycle_start_date, '%Y-%m') AS m FROM t"
    await DorisClient(host="h", port=1, user="u", password="p", database="d").query(sql)

    assert len(calls) == 1, calls
    args = calls[0]
    assert args == (sql,), (
        "expected cur.execute(sql) with no args, got %r — empty params "
        "trigger pymysql %-formatting and break '%Y-%m'" % (args,)
    )


@pytest.mark.asyncio
async def test_query_with_params_preserved(monkeypatch):
    """Explicit params still reach pymysql as a second argument."""
    from wren_agent.tools.doris_client import DorisClient

    calls: list = []
    _patch_pymysql(monkeypatch, calls)

    await DorisClient(host="h", port=1, user="u", password="p", database="d").query(
        "SELECT * FROM t WHERE id = %s", (42,)
    )

    assert len(calls) == 1, calls
    assert calls[0] == ("SELECT * FROM t WHERE id = %s", (42,))


# ── Fix 2: cross-layer failure remap ───────────────────────────


def _plan_with_tables() -> DWDesignPlan:
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s",
        summary_table="dws_order_payment_org_monthly",
        source_dwd_table="dwd_x",
    )]
    plan.ads_designs = [ADSDesign(
        application_name="a",
        ads_table="ads_order_payment_org_monthly",
    )]
    return plan


def _doris_explain_failure(detail: str) -> ValidationResult:
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DORIS_EXPLAIN",
        passed=False,
        checks=[],
        failures=[{"check": "doris_dml_explain", "detail": detail}],
        semantic_assertions=[],
    )


def test_remap_doris_explain_to_owning_layer():
    """A DORIS_EXPLAIN failure on a plan DWS table must be remapped to DWS."""
    from wren_agent.nodes.validation import _remap_cross_layer_failures

    failure = _doris_explain_failure(
        "db_dws.dws_order_payment_org_monthly load SQL rejected "
        "by Doris: not enough arguments for format string"
    )
    remapped = _remap_cross_layer_failures({}, _plan_with_tables(), [failure])

    assert len(remapped) == 1
    assert remapped[0].layer == "DWS"
    assert remapped[0].failures == failure.failures


def test_remap_ads_failure():
    from wren_agent.nodes.validation import _remap_cross_layer_failures

    failure = _doris_explain_failure(
        "db_ads.ads_order_payment_org_monthly load SQL rejected "
        "by Doris: some error"
    )
    remapped = _remap_cross_layer_failures({}, _plan_with_tables(), [failure])

    assert len(remapped) == 1
    assert remapped[0].layer == "ADS"


def test_remap_unknown_table_stays_original_layer():
    """A failure whose table matches no plan target keeps its layer — nothing
    is silently dropped."""
    from wren_agent.nodes.validation import _remap_cross_layer_failures

    failure = _doris_explain_failure(
        "some_other_table load SQL rejected by Doris: boom"
    )
    remapped = _remap_cross_layer_failures({}, _plan_with_tables(), [failure])

    assert len(remapped) == 1
    assert remapped[0].layer == "DORIS_EXPLAIN"


def test_remap_plan_layer_not_remapped():
    """PLAN failures stay PLAN — they name source tables, not plan targets."""
    from wren_agent.nodes.validation import _remap_cross_layer_failures

    failure = ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="PLAN",
        passed=False,
        failures=[{"check": "requirement_coverage", "detail": "gap"}],
        checks=[],
        semantic_assertions=[],
    )
    remapped = _remap_cross_layer_failures({}, _plan_with_tables(), [failure])

    assert len(remapped) == 1
    assert remapped[0].layer == "PLAN"
    assert remapped[0].failures == failure.failures


# ── Fix 2 end-to-end: design_revise routes DORIS_EXPLAIN to repair ──


@pytest.mark.asyncio
async def test_design_revise_repairs_doris_explain_failure(monkeypatch):
    """A DORIS_EXPLAIN failure must reach the repair LLM (layer remapped to
    DWS) and apply the fix — not short-circuit into a silent no-op."""
    from wren_agent.nodes.validation import design_revise

    plan = _plan_with_tables()
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS",
            target_table="dws_order_payment_org_monthly",
            sql_type="DML_INSERT",
            sql="INSERT INTO dws_order_payment_org_monthly (stat_month, org_id) "
                "SELECT DATE_FORMAT(a.cycle_start_date, '%Y-%m'), a.org_id FROM dwd_x a",
        ),
    ]

    state = create_initial_state("t", "goal", [], {})
    state["design_plan"] = plan
    state["design_revision_count"] = 0
    state["warnings"] = []
    state["validation_results"] = [
        _doris_explain_failure(
            "db_dws.dws_order_payment_org_monthly load SQL rejected "
            "by Doris: not enough arguments for format string"
        ),
    ]

    repaired_sql = (
        "INSERT INTO dws_order_payment_org_monthly (stat_month, org_id) "
        "SELECT DATE_FORMAT(a.cycle_start_date, '%Y-%m'), a.org_id FROM dwd_x a"
    )

    async def fake_generate_typed_json(self, system_prompt, user_message, schema, *args, **kw):
        assert "DWS" in system_prompt or "DWS" in user_message
        import json as _json
        raw = _json.dumps({
            "repairs": [{
                "target_table": "dws_order_payment_org_monthly",
                "sql_type": "DML_INSERT",
                "sql": repaired_sql,
            }]
        })
        return schema.model_validate_json(raw)

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client",
        lambda: type("FakeLLM", (), {"generate_typed_json": fake_generate_typed_json})(),
    )

    new_state = await design_revise(state)

    # The repair LLM was called and the artifact was actually replaced.
    artifact = plan.sql_artifacts[0]
    assert artifact.sql == repaired_sql
    assert artifact.version == 2
    assert any(
        "LLM repaired 1/1 failed layers" in w for w in new_state["warnings"]
    )
    # audit log records the remapped layer (DWS), not the synthetic one
    revise_logs = [
        entry for entry in new_state["audit_log"]
        if entry.get("phase") == "design_revise"
    ]
    assert revise_logs and "DWS" in revise_logs[0]["failed_layers"]


# ── Fix 3: SELECT output aliases no longer flagged as source columns ──
# (task d28431de: `GROUP BY stat_month` — a SELECT alias — failed every
# DWS/ADS load SQL across two revision rounds and killed the task)


def test_bare_column_scan_ignores_select_aliases():
    from wren_agent.nodes.validation import _bare_source_column_candidates

    dml = """INSERT INTO db_dws.dws_order_collection_unit_month (org_id, stat_month, amount_receivable)
SELECT
       a.org_id AS org_id,
       DATE_FORMAT(a.cycle_start_date, '%Y-%m') AS stat_month,
       SUM(a.amount_receivable) AS amount_receivable
FROM db_dwd.dwd_order_payment_cycle_acc a
WHERE a.cycle_start_date IS NOT NULL
GROUP BY stat_month, org_id"""
    # `stat_month` is a SELECT output alias used in GROUP BY — must NOT be
    # flagged as a hallucinated source column.
    assert "stat_month" not in _bare_source_column_candidates(dml)
    assert "org_id" not in _bare_source_column_candidates(dml)


def test_bare_column_scan_still_catches_real_missing_column():
    from wren_agent.nodes.validation import _bare_source_column_candidates

    dml = ("INSERT INTO dws_x (amount) SELECT amount_receivable FROM dwd_a "
           "WHERE is_deleted_flag = 0")
    assert "is_deleted_flag" in _bare_source_column_candidates(dml)


def test_bare_column_scan_implicit_alias():
    from wren_agent.nodes.validation import _bare_source_column_candidates

    dml = ("INSERT INTO dws_x (stat_month) SELECT DATE_FORMAT(a.d, '%Y-%m') "
           "stat_month FROM dwd_a a GROUP BY stat_month")
    assert "stat_month" not in _bare_source_column_candidates(dml)


def test_bare_column_scan_keeps_passthrough_alias():
    """`org_name AS org_name` is a source column, not just an output alias.

    Task 9f42f542: subtracting every SELECT alias dropped `col AS col` so
    `org_name` (absent from DWS) never failed `warehouse_source_columns_exist`.
    """
    from wren_agent.nodes.validation import _bare_source_column_candidates

    dml = (
        "INSERT INTO ads_x (inventory_month, org_id, org_name) "
        "SELECT stat_date AS inventory_month, org_id, org_name AS org_name "
        "FROM dws_x"
    )
    found = _bare_source_column_candidates(dml)
    assert "org_name" in found
    assert "stat_date" in found
    assert "inventory_month" not in found


# ── Fix 4: no-progress guard does not hard-fail known probe misfires ──


def _dws_explain_failure(detail):
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DORIS_EXPLAIN",
        passed=False,
        failures=[{"check": "doris_dml_explain", "detail": detail}],
        checks=[],
        semantic_assertions=[],
    )


def _col_misfire_failure(detail):
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DWS",
        passed=False,
        failures=[{"check": "warehouse_source_columns_exist", "detail": detail}],
        checks=[],
        semantic_assertions=[],
    )


def test_no_progress_guard_skips_format_string_misfire():
    from wren_agent.nodes.validation import _is_known_probe_misfire

    results = [_dws_explain_failure(
        "db_dws.dws_x load SQL rejected by Doris: not enough arguments "
        "for format string"
    )]
    assert _is_known_probe_misfire(results) is True


def test_no_progress_guard_skips_alias_column_misfire():
    from wren_agent.nodes.validation import _is_known_probe_misfire

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.sql_artifacts = [SQLArtifact(
        layer="DWS", target_table="dws_x", sql_type="DML_INSERT",
        sql="INSERT INTO dws_x (stat_month) SELECT DATE_FORMAT(a.d, '%Y-%m') "
            "AS stat_month FROM dwd_a a GROUP BY stat_month",
    )]
    detail = ("db_dws.dws_x references columns absent from explored source "
              "`dwd_a`: ['stat_month']. Available columns: ...")
    assert _is_known_probe_misfire([_col_misfire_failure(detail)], plan) is True


def test_no_progress_guard_real_failure_not_misfire():
    from wren_agent.nodes.validation import _is_known_probe_misfire

    detail = ("db_dws.dws_x references columns absent from explored source "
              "`dwd_a`: ['hallucinated_col']. Available columns: ...")
    assert _is_known_probe_misfire([_col_misfire_failure(detail)]) is False


# ── Fix 5: layer_relevant_db_names covers the FULL layer database list ──
# (task d28431de: registry queries were scoped to the landing db `db_dws`,
# which matched no registry rows — the dws-layer metrics never reached the
# prompts)


def _warehouse_state(layers: dict, landing: dict) -> dict:
    from wren_agent.models.task import SourceDatabase
    from wren_agent.state import create_initial_state

    sdb = SourceDatabase(
        id="s1", name="wh", db_type="doris",
        host="h", port=9030, database="wren_dw",
        mcp_server_name="mcp_wh",
        connection_params={"layers": layers, "landing": landing},
    )
    return create_initial_state(
        "t", "goal", [sdb], {}, source_mode="warehouse")


def test_layer_relevant_db_names_full_layer_list():
    from wren_agent.nodes.deployment import layer_relevant_db_names

    layers = {
        "dws": ["db_dws", "dws_ecommerce_orders_GZ", "dws_ecommerce_orders_sh"],
        "dim": ["db_dim", "dim_ecommerce_shop_sh"],
        "dwd": ["db_dwd", "dwd_ecommerce_orders_sh"],
    }
    state = _warehouse_state(layers, {"dws": "db_dws"})
    dbs = layer_relevant_db_names(state, {"dws"})
    # landing db + all catalog schemas of the dws layer, not just `db_dws`
    assert "db_dws" in dbs
    assert "dws_ecommerce_orders_GZ" in dbs
    assert "dws_ecommerce_orders_sh" in dbs


def test_layer_relevant_db_names_business_mode_empty():
    from wren_agent.nodes.deployment import layer_relevant_db_names
    from wren_agent.state import create_initial_state

    state = create_initial_state("t", "goal", [], {})
    assert layer_relevant_db_names(state, {"dws"}) == set()


# ── Fix 6: injection sections land BEFORE the schema dump (anchor) ──


def test_inject_into_user_message_uses_anchor():
    from wren_agent.nodes.design_layers import (
        _INJECTION_ANCHOR,
        _inject_into_user_message,
    )

    msg = f"## Target Data Warehouse\n...\n{_INJECTION_ANCHOR}\n## Source Schema Explorations\n..."
    out = _inject_into_user_message(msg, "## Registered Metrics\n- x")
    assert out.index("## Registered Metrics") < out.index("## Source Schema")
    assert _INJECTION_ANCHOR in out  # anchor survives for later sections


def test_inject_into_user_message_appends_when_anchor_gone():
    from wren_agent.nodes.design_layers import _inject_into_user_message

    out = _inject_into_user_message("## Task\n...", "## Registered Metrics\n- x")
    assert out == "## Task\n...## Registered Metrics\n- x"


# ── Fix 7: semantic assertions retry once on empty response ──


@pytest.mark.asyncio
async def test_semantic_assertions_retries_on_empty(monkeypatch):
    from wren_agent.nodes.validation import _generate_semantic_assertions_batch

    calls = {"n": 0}

    async def fake_generate_json(self, system_prompt, user_message, *, token_kind=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # empty first response
        return ('{"by_artifact": [{"index": 0, "assertions": '
                '[{"metric": "amount", "expected_range": "> 0", "source": "r"}]}]}')

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client",
        lambda: type("FakeLLM", (), {"generate_json": fake_generate_json})(),
    )

    state = create_initial_state("t", "goal", [], {})
    artifacts = [SQLArtifact(
        layer="DWS", target_table="dws_x", sql_type="DML_INSERT",
        sql="INSERT INTO dws_x SELECT 1",
    )]
    results = await _generate_semantic_assertions_batch(artifacts, state)
    assert calls["n"] == 2  # retried once
    assert any("amount" in str(a.get("metric")) for a in results[0].semantic_assertions)
