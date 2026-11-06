"""Fill-rate contract across core_model publish and warehouse exploration.

Task bb58438f: core modeling copied ODS `inventory_date` (100% NULL) into
DWD. Warehouse grill asked how to extract a month from it; the compiler
rendered DATE_FORMAT + IS NOT NULL; DAG succeeded with 0 rows.

Explore fills FieldSchema.null_ratio; prompts/grill surface EMPTY dates;
harness rejects them as grain; quality_monitor WARNs (does not fail publish).
"""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDDesign, DWDesignPlan, SQLArtifact
from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.models.task import ExecutionResult, TaskStatus
from wren_agent.state import create_initial_state
from wren_agent.tools.column_profile import (
    UNUSABLE_NULL_RATIO,
    attach_null_ratios,
    format_null_mark,
    grain_columns_in_sql,
    is_profile_candidate,
    is_unusable_grain,
    populated_alternatives,
)


def _dwd_inventory_table(**ratios) -> TableSchema:
    fields = [
        FieldSchema(name="org_id", data_type="BIGINT"),
        FieldSchema(
            name="inventory_date", data_type="DATE",
            null_ratio=ratios.get("inventory_date"),
        ),
        FieldSchema(
            name="create_time", data_type="DATETIME",
            null_ratio=ratios.get("create_time"),
        ),
        FieldSchema(name="sku_id", data_type="BIGINT"),
    ]
    return TableSchema(
        name="dwd_stock_inventory_info",
        schema_name="dwd_gz_test",
        db_type="doris",
        row_count_estimate=47965,
        fields=fields,
    )


def _warehouse_state_with_profiles() -> dict:
    state = create_initial_state("t", "按组织按月统计库存盘点", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dim": ["dim_gz_test"],
                "dwd": ["dwd_gz_test"],
                "dws": ["dws_gz_test"],
                "ads": ["ads_gz_test"],
            },
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd",
            tables=[_dwd_inventory_table(inventory_date=1.0, create_time=0.0)],
        ),
        "wh#dim": SchemaExploration(
            source_db_id="wh#dim",
            tables=[TableSchema(
                name="dim_sku", schema_name="dim_gz_test",
                db_type="doris", row_count_estimate=100,
                fields=[
                    FieldSchema(name="sku_id", data_type="BIGINT"),
                    FieldSchema(name="is_current", data_type="TINYINT"),
                ],
            )],
        ),
    }
    return state


BB58438F_SQL = """
INSERT INTO dws_gz_test.dws_stock_inventory_monthly
  (stat_month, org_id, asset_count)
SELECT DATE_FORMAT(a.inventory_date, '%Y-%m') AS stat_month,
       a.org_id, COUNT(*)
FROM dwd_gz_test.dwd_stock_inventory_info a
JOIN dim_gz_test.dim_sku b
  ON a.sku_id = b.sku_id AND b.is_current = 1
WHERE a.org_id IS NOT NULL AND a.inventory_date IS NOT NULL
GROUP BY 1, 2
"""


def test_profile_candidates_include_date_suffix_and_create_time():
    assert is_profile_candidate(FieldSchema(name="inventory_date", data_type="VARCHAR(20)"))
    assert is_profile_candidate(FieldSchema(name="create_time", data_type="DATETIME"))
    assert is_profile_candidate({"name": "stat_month", "type": "VARCHAR(7)"})
    assert not is_profile_candidate(FieldSchema(name="org_id", data_type="BIGINT"))
    assert not is_profile_candidate({"name": "drop table; --", "type": "DATE"})


def test_unusable_grain_threshold_and_prompt_mark():
    assert UNUSABLE_NULL_RATIO == 0.99
    assert is_unusable_grain(1.0)
    assert is_unusable_grain(0.99)
    assert not is_unusable_grain(0.5)
    assert not is_unusable_grain(None)
    assert "EMPTY, do NOT use as grain" in format_null_mark(1.0)
    assert format_null_mark(0.0) == " [null_rate=0%]"
    assert format_null_mark(None) == ""


def test_populated_alternatives_skips_empty_and_unprofiled():
    fields = _dwd_inventory_table(inventory_date=1.0, create_time=0.02).fields
    alts = populated_alternatives(fields, exclude="inventory_date")
    assert alts == ["create_time (null_rate=2%)"]


def test_grain_columns_extracted_from_time_fn_and_not_null():
    found = grain_columns_in_sql(BB58438F_SQL)
    cols = {(q, c) for q, c in found}
    assert ("a", "inventory_date") in cols
    assert ("a", "org_id") in cols  # IS NOT NULL — harness ignores unprofiled keys


@pytest.mark.asyncio
async def test_attach_null_ratios_one_query():
    table = _dwd_inventory_table()
    seen = []

    async def query_one(sql):
        seen.append(sql)
        return {"__n": 100, "n0": 100, "n1": 2}

    await attach_null_ratios(table, query_one)
    assert len(seen) == 1
    assert "`dwd_gz_test`.`dwd_stock_inventory_info`" in seen[0]
    by_name = {f.name: f.null_ratio for f in table.fields}
    assert by_name["inventory_date"] == 1.0
    assert by_name["create_time"] == 0.02
    assert by_name["org_id"] is None


def test_design_prompt_marks_empty_date_column():
    from wren_agent.nodes.design_layers import (
        _serialize_explorations_for_prompt,
        _warehouse_sql_hard_rules,
    )

    state = _warehouse_state_with_profiles()
    text = _serialize_explorations_for_prompt(state)
    assert "inventory_date" in text
    assert "null_rate=100% — EMPTY, do NOT use as grain" in text
    assert "create_time" in text
    assert "null_rate=0%" in text
    rules = _warehouse_sql_hard_rules()
    assert "null_rate=100% — EMPTY" in rules
    assert "bb58438f" in rules


def test_grill_summary_lists_empty_and_populated_dates():
    from wren_agent.nodes.grill import _build_exploration_summary

    state = _warehouse_state_with_profiles()
    text = _build_exploration_summary(state)
    assert "EMPTY date columns" in text
    assert "inventory_date" in text
    assert "Populated date columns" in text
    assert "create_time" in text


def test_harness_rejects_date_format_on_empty_grain():
    from wren_agent.nodes.validation import _validate_warehouse_load_sql

    state = _warehouse_state_with_profiles()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    artifact = SQLArtifact(
        layer="DWS", target_table="dws_stock_inventory_monthly",
        sql_type="DML_INSERT", sql=BB58438F_SQL,
    )
    checks = _validate_warehouse_load_sql(
        "dws_stock_inventory_monthly", [artifact], state, plan,
    )
    grain = [
        f for f in checks if f.get("check") == "warehouse_grain_column_empty"
    ]
    assert grain, f"expected warehouse_grain_column_empty, got: {checks}"
    detail = grain[0]["detail"]
    assert "inventory_date" in detail
    assert "100%" in detail
    assert "create_time" in detail


def test_harness_accepts_populated_create_time_grain():
    from wren_agent.nodes.validation import _validate_warehouse_load_sql

    state = _warehouse_state_with_profiles()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    sql = BB58438F_SQL.replace("a.inventory_date", "a.create_time")
    artifact = SQLArtifact(
        layer="DWS", target_table="dws_stock_inventory_monthly",
        sql_type="DML_INSERT", sql=sql,
    )
    checks = _validate_warehouse_load_sql(
        "dws_stock_inventory_monthly", [artifact], state, plan,
    )
    assert not any(
        f.get("check") == "warehouse_grain_column_empty" for f in checks
    ), f"populated create_time must pass, got: {checks}"


def test_repair_hint_for_empty_grain_column():
    from wren_agent.nodes.validation import _repair_hint

    hint = _repair_hint({"check": "warehouse_grain_column_empty", "detail": "x"})
    assert "create_time" in hint
    assert "IS NOT NULL" in hint
    assert "≥99%" in hint or "99%" in hint


@pytest.mark.asyncio
async def test_quality_monitor_empty_date_is_warn_not_fail(monkeypatch):
    """Empty ODS dates copied into DWD must not fail core_model publish."""
    from wren_agent.nodes import quality_monitor as qm

    class FakeDoris:
        def __init__(self, *args, **kwargs):
            pass

        async def get_row_count(self, table):
            return 100

        async def query_value(self, sql):
            if "IS NULL" in sql.upper():
                return 0
            return 100

        async def query_one(self, sql):
            return {"__n": 47965, "n0": 47965, "n1": 0}

    async def fake_handoff(state):
        state["_handoff_called"] = True
        return {"published": True}

    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", FakeDoris)
    monkeypatch.setattr(
        "wren_agent.tools.core_handoff.finalize_core_handoff", fake_handoff,
    )

    state = create_initial_state("t", "核心建模", [], {}, source_mode="business_db")
    state["business_task_type"] = "core_model"
    state["landing"] = {
        "ods": "ods_gz_test", "dim": "dim_gz_test", "dwd": "dwd_gz_test",
    }
    state["execution_results"] = [
        ExecutionResult(task_instance_id="x", success=True),
    ]
    plan = DWDesignPlan(task_id="t", modeling_goal="核心建模")
    plan.dwd_designs = [DWDDesign(
        fact_name="stock_inventory",
        fact_table="dwd_gz_test.dwd_stock_inventory_info",
        fields=[
            {"name": "inventory_date", "type": "DATE"},
            {"name": "create_time", "type": "DATETIME"},
            {"name": "org_id", "type": "BIGINT"},
        ],
    )]
    state["design_plan"] = plan

    result = await qm.quality_monitor(state)
    checks = (result.get("quality_checks") or {}).get("checks") or []
    date_checks = [
        c for c in checks
        if c.get("column") == "inventory_date" and c.get("type") == "null_ratio"
    ]
    assert date_checks, f"expected inventory_date warn, got: {checks}"
    assert all(c.get("status") == "warn" for c in date_checks)
    assert (result.get("quality_checks") or {}).get("failed", 1) == 0
    assert result.get("status") == TaskStatus.COMPLETED
    assert result.get("_handoff_called") is True
    assert "create_time" in date_checks[0].get("detail", "")
