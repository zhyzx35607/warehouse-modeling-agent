"""Tests for plain-language failure reports, non-blocking LLM design
review, revision-budget-aware invariant gating, and timeline dedupe."""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDesignPlan, DWSDesign, SQLArtifact
from wren_agent.models.task import (
    ApprovalItem,
    ApprovalStatus,
    ExecutionResult,
    TaskStatus,
    ValidationLevel,
    ValidationResult,
)
from wren_agent.state import create_initial_state
from tests.test_materialization_integrity import _valid_materialized_plan


# ── translate_error ──────────────────────────────────────────────


def test_translate_error_known_rules():
    from wren_agent.nodes.reporting import translate_error

    assert "没有找到可用的数据表" in translate_error(
        "Schema exploration produced no tables from 1 source database(s)"
    )
    assert "必须修正的问题" in translate_error(
        "Critical design invariants failed; task cannot enter approval"
    )
    assert "人工介入" in translate_error(
        "auto mode: execution errors persisted after 2 auto-approved rounds"
    )


def test_translate_error_fallback_keeps_original():
    from wren_agent.nodes.reporting import translate_error

    assert translate_error("some exotic failure") == "some exotic failure（技术细节）"


# ── build_failure_report ─────────────────────────────────────────


def test_build_failure_report_from_persisted_dict():
    """The /report fallback feeds raw load_task_artifacts dicts — the
    builder must tolerate dicts instead of pydantic models."""
    from wren_agent.nodes.reporting import build_failure_report

    report = build_failure_report({
        "task_id": "t-1",
        "modeling_goal": "统计近两年的租金情况",
        "phase": "harness_validate",
        "errors": ["Critical design invariants failed; task cannot enter approval"],
        "node_timeline": [
            {"node": "receive_task", "status": "completed"},
            {"node": "design_dws", "status": "completed"},
            {"node": "harness_validate", "status": "failed"},
        ],
        "design_plan": {
            "dws_designs": [{"summary_table": "dws_rent_monthly"}],
            "ads_designs": [{"ads_table": "ads_rent_summary"}],
        },
        "validation_results": [{
            "layer": "DWS", "level": "structure", "passed": False,
            "failures": [{"check": "materialization_sql_complete",
                          "detail": "dws_rent_monthly must have both DDL and DML load SQL"}],
        }],
    })

    assert "# 任务执行报告（未完成）" in report
    assert "统计近两年的租金情况" in report
    assert "必须修正的问题" in report  # translated error
    assert "设计汇总层（DWS）" in report  # completed step label
    assert "dws_rent_monthly" in report  # designed tables listed
    assert "materialization_sql_complete" in report  # problem list table
    assert "## 建议下一步" in report


def test_build_failure_report_no_errors_no_timeline():
    from wren_agent.nodes.reporting import build_failure_report

    report = build_failure_report({"task_id": "t-2", "modeling_goal": "g"})
    assert "系统未记录具体错误信息" in report
    assert "尚未完成可记录的步骤" in report


# ── generate_report failure branch ───────────────────────────────


@pytest.mark.asyncio
async def test_generate_report_early_failure_produces_failure_report():
    from wren_agent.nodes.reporting import generate_report

    state = create_initial_state("t-fail", "统计欠费情况", [], {})
    state["status"] = TaskStatus.FAILED
    state["errors"] = [
        "Schema exploration produced no tables from 1 source database(s)"]
    state["phase"] = "schema_explore_light"

    result = await generate_report(state)

    report = result["construction_report"]
    assert result["status"] == TaskStatus.FAILED
    assert "# 任务执行报告（未完成）" in report
    assert "没有找到可用的数据表" in report
    assert "## 建议下一步" in report


@pytest.mark.asyncio
async def test_generate_report_post_execution_failure_keeps_full_report(monkeypatch):
    from wren_agent.nodes.reporting import generate_report

    class FakeLLM:
        async def chat(self, *_args, **_kwargs):
            return "- Failed truthfully."

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client", lambda: FakeLLM()
    )

    state = create_initial_state("t-late-fail", "goal", [], {})
    state["status"] = TaskStatus.FAILED
    state["design_plan"] = _valid_materialized_plan()
    state["execution_results"] = [
        ExecutionResult(task_instance_id="1", success=True)]
    state["quality_checks"] = {"failed": 1, "errored": 0, "total": 1}

    result = await generate_report(state)

    # Full construction report, not the failure template
    assert "数仓建模建设报告" in result["construction_report"]
    assert "任务执行报告（未完成）" not in result["construction_report"]


@pytest.mark.asyncio
async def test_generate_report_owns_rejected_drift_failed_flag():
    """route_after_approval can't mutate state (LangGraph discards route
    mutations) — the report node must flag rejected breaking drifts."""
    from wren_agent.nodes.reporting import generate_report

    state = create_initial_state("t-drift", "goal", [], {})
    state["approval_context"] = "schema_drift"
    item = ApprovalItem(title="drift", description="breaking",
                        artifact_type="drift_fix", artifact_id="orders")
    item.status = ApprovalStatus.REJECTED
    state["approval_items"] = [item]

    result = await generate_report(state)

    assert result["status"] == TaskStatus.FAILED
    assert any("Breaking schema drift" in e for e in result["errors"])
    assert "任务执行报告（未完成）" in result["construction_report"]


# ── invariant gating gives design_revise a chance ────────────────


def _invariant_failing_plan() -> DWDesignPlan:
    """DWS design with DDL but no DML load SQL → materialization_sql_complete."""
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s", summary_table="dws_x",
        source_dwd_table="fct_x",
        fields=[{"name": "org_id", "type": "VARCHAR(255)"}],
        group_by_dimensions=["org_id"], grain=["org_id"],
        requirement_coverage=[{
            "requirement": "organization grain",
            "status": "supported",
            "evidence": ["org_id"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [SQLArtifact(
        layer="DWS", target_table="dws_x", sql_type="DDL",
        sql="CREATE TABLE dws_x (org_id VARCHAR(255)) UNIQUE KEY(org_id)",
    )]
    return plan


@pytest.mark.asyncio
async def test_single_invariant_failure_within_budget_blocks_but_does_not_fail(monkeypatch):
    from wren_agent.nodes import validation
    from wren_agent.nodes.validation import harness_validate

    async def fake_structure(_artifact):
        return ValidationResult(level=ValidationLevel.STRUCTURE, layer="DWS", passed=True)

    async def fake_semantic_batch(_artifacts, _state):
        return [
            ValidationResult(level=ValidationLevel.SEMANTIC, layer="DWS", passed=None)
        ]

    monkeypatch.setattr(validation, "_validate_structure", fake_structure)
    monkeypatch.setattr(
        validation, "_generate_semantic_assertions_batch", fake_semantic_batch,
    )

    state = create_initial_state("t-revise", "goal", [], {})
    state["design_plan"] = _invariant_failing_plan()

    result = await harness_validate(state)

    assert result["all_validations_passed"] is False
    assert result["validation_blocking"] is True
    assert result["validation_error_count"] == 1
    assert result["status"] != TaskStatus.FAILED


@pytest.mark.asyncio
async def test_single_validation_error_blocks_approval(monkeypatch):
    from wren_agent.nodes import validation
    from wren_agent.nodes.validation import harness_validate

    async def fake_structure(_artifact):
        return ValidationResult(
            level=ValidationLevel.STRUCTURE, layer="DWS", passed=True
        )

    async def fake_consistency(_artifact, _plan):
        return ValidationResult(
            level=ValidationLevel.CONSISTENCY, layer="DWS", passed=True
        )

    async def fake_semantic_batch(_artifacts, _state):
        return [
            ValidationResult(level=ValidationLevel.SEMANTIC, layer="DWS", passed=None)
        ]

    one_error = ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DWS",
        passed=False,
        failures=[{"check": "one_defect", "detail": "single blocking defect"}],
    )
    monkeypatch.setattr(
        validation, "_validate_materialization_plan", lambda *_: [one_error]
    )
    monkeypatch.setattr(validation, "_validate_structure", fake_structure)
    monkeypatch.setattr(validation, "_validate_consistency", fake_consistency)
    monkeypatch.setattr(
        validation, "_generate_semantic_assertions_batch", fake_semantic_batch,
    )
    monkeypatch.setattr(validation, "_validate_design_shape", lambda *_: None)

    state = create_initial_state("t-block", "goal", [], {})
    state["design_plan"] = _valid_materialized_plan()
    result = await harness_validate(state)

    assert result["validation_error_count"] == 1
    assert result["all_validations_passed"] is False
    assert result["validation_blocking"] is True
    assert result["status"] != TaskStatus.FAILED


def test_plain_report_treats_validation_failure_as_needs_attention():
    from wren_agent.nodes.reporting import build_plain_report

    state = create_initial_state("t-present", "生成订单金额月度汇总", [], {})
    state["status"] = TaskStatus.COMPLETED
    state["design_plan"] = _valid_materialized_plan()
    state["validation_error_count"] = 1
    state["validation_results"] = [ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DWS",
        passed=False,
        failures=[{"check": "one_defect", "detail": "blocking"}],
    )]

    report = build_plain_report(state)

    assert "当前结论**：需要处理" in report
    assert "尚未达到可直接使用的标准" in report
    assert "已完成（有提示）" not in report


@pytest.mark.asyncio
async def test_invariant_failure_with_exhausted_budget_fails_fast(monkeypatch):
    from wren_agent.graph import MAX_DESIGN_REVISIONS
    from wren_agent.nodes import validation
    from wren_agent.nodes.validation import harness_validate

    async def fake_structure(_artifact):
        return ValidationResult(level=ValidationLevel.STRUCTURE, layer="DWS", passed=True)

    async def fake_semantic_batch(_artifacts, _state):
        return [
            ValidationResult(level=ValidationLevel.SEMANTIC, layer="DWS", passed=None)
        ]

    monkeypatch.setattr(validation, "_validate_structure", fake_structure)
    monkeypatch.setattr(
        validation, "_generate_semantic_assertions_batch", fake_semantic_batch,
    )
    monkeypatch.setattr(
        validation,
        "_validate_materialization_plan",
        lambda *_: [ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer="DWS",
            passed=False,
            failures=[
                {"check": "first_defect", "detail": "blocking defect"},
            ],
        )],
    )

    state = create_initial_state("t-exhausted", "goal", [], {})
    state["design_plan"] = _invariant_failing_plan()
    state["design_revision_count"] = MAX_DESIGN_REVISIONS

    result = await harness_validate(state)

    assert result["status"] == TaskStatus.FAILED
    assert any("Critical design invariants failed" in e for e in result["errors"])


# ── LLM design review is advisory, not blocking ──────────────────


@pytest.mark.asyncio
async def test_llm_review_error_issues_do_not_block(monkeypatch):
    from wren_agent.nodes import validation
    from wren_agent.nodes.validation import harness_validate

    async def fake_structure(_artifact):
        return ValidationResult(level=ValidationLevel.STRUCTURE, layer="DWS", passed=True)

    async def fake_consistency(_artifact, _plan):
        return ValidationResult(level=ValidationLevel.CONSISTENCY, layer="DWS", passed=True)

    async def fake_semantic_batch(_artifacts, _state):
        return [
            ValidationResult(level=ValidationLevel.SEMANTIC, layer="DWS", passed=None)
        ]

    monkeypatch.setattr(validation, "_validate_structure", fake_structure)
    monkeypatch.setattr(validation, "_validate_consistency", fake_consistency)
    monkeypatch.setattr(
        validation, "_generate_semantic_assertions_batch", fake_semantic_batch,
    )
    # The fixture's single-line DDLs defeat the line-based shape parser;
    # shape checking is not what this test exercises.
    monkeypatch.setattr(validation, "_validate_design_shape", lambda *a, **k: None)

    state = create_initial_state("t-review", "goal", [], {})
    state["design_plan"] = _valid_materialized_plan()
    state["design_review_issues"] = [{
        "layer": "ADS", "severity": "error",
        "rule_id": "ads-metric-naming-convention",
        "detail": "Metric names should follow the convention",
    }]

    result = await harness_validate(state)

    assert result["status"] != TaskStatus.FAILED
    assert result["all_validations_passed"] is True
    review = [r for r in result["validation_results"] if r.layer == "DESIGN_REVIEW"]
    assert review and review[0].passed is None  # visible but non-blocking


# ── node_timeline dedupe ─────────────────────────────────────────


def test_dedupe_node_timeline_latest_wins_order_preserved():
    from wren_agent.tools.pg_store import _dedupe_node_timeline

    timeline = [
        {"node": "a", "status": "completed", "duration_ms": 1},
        {"node": "b", "status": "completed", "duration_ms": 2},
        {"node": "a", "status": "completed", "duration_ms": 3},
        {"node": "c", "status": "failed"},
    ]
    deduped = _dedupe_node_timeline(timeline)

    assert [e["node"] for e in deduped] == ["a", "b", "c"]
    assert deduped[0]["duration_ms"] == 3  # latest entry wins


def test_dedupe_node_timeline_empty_and_none():
    from wren_agent.tools.pg_store import _dedupe_node_timeline

    assert _dedupe_node_timeline(None) == []
    assert _dedupe_node_timeline([]) == []


# ── warehouse source-metadata backfill ───────────────────────────


def _warehouse_state_with_plan():
    """Warehouse-mode state whose DWS design has empty source_tables but
    whose DML references the right (bare) tables."""
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema

    state = create_initial_state("t-wh", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd",
            phase="light",
            tables=[TableSchema(
                name="dwd_order_payment_cycle_acc",
                schema_name="db_dwd",
                db_type="doris",
                fields=[FieldSchema(name="amount_receivable", data_type="DECIMAL")],
            )],
        ),
    }
    plan = DWDesignPlan(task_id="t-wh", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s", summary_table="dws_collection_monthly",
        source_dwd_table="dwd_order_payment_cycle_acc",
        source_tables=[],  # LLM left it empty
        grain=["stat_month"],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_collection_monthly", sql_type="DDL",
            sql="CREATE TABLE dws_collection_monthly (stat_month VARCHAR(7)) UNIQUE KEY(stat_month)",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_collection_monthly", sql_type="DML_INSERT",
            sql="INSERT INTO dws_collection_monthly SELECT DATE_FORMAT(cycle_start_date,'%Y-%m') "
                "FROM dwd_order_payment_cycle_acc",
        ),
    ]
    state["design_plan"] = plan
    return state, plan


def test_rewrite_fixes_wrong_warehouse_db_prefix():
    from wren_agent.models.design import DWSDesign, DWDesignPlan, SQLArtifact
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.validation import _rewrite_warehouse_plan_table_refs
    from wren_agent.state import create_initial_state

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    exp = SchemaExploration(source_db_id="wh#dws", phase="light")
    exp.tables.append(TableSchema(
        name="dws_order_payment_daily",
        schema_name="dws_ecommerce_orders_bj",
        db_type="doris",
        fields=[FieldSchema(name="stat_date", data_type="DATE")],
    ))
    state["explorations"] = {"wh#dws": exp}

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s",
        summary_table="dws_order_receivable_monthly",
        source_dwd_table="dws_order_payment_daily",
        source_tables=["db_dws.dws_order_payment_daily"],
        grain=["stat_month"],
    )]
    plan.sql_artifacts = [SQLArtifact(
        layer="DWS",
        target_table="dws_order_receivable_monthly",
        sql_type="DML_INSERT",
        sql=(
            "INSERT INTO dws_order_receivable_monthly "
            "SELECT * FROM db_dws.dws_order_payment_daily t"
        ),
    )]
    state["design_plan"] = plan

    n = _rewrite_warehouse_plan_table_refs(state, plan)
    assert n >= 1
    assert "dws_ecommerce_orders_bj.dws_order_payment_daily" in plan.sql_artifacts[0].sql
    assert "db_dws." not in plan.sql_artifacts[0].sql
    assert plan.dws_designs[0].source_tables == [
        "dws_ecommerce_orders_bj.dws_order_payment_daily"
    ]


def test_backfill_fills_empty_source_tables_from_dml():
    from wren_agent.nodes.validation import _backfill_warehouse_source_metadata

    state, plan = _warehouse_state_with_plan()
    _backfill_warehouse_source_metadata(state, plan)

    assert plan.dws_designs[0].source_tables == [
        "db_dwd.dwd_order_payment_cycle_acc"
    ]


def test_backfill_keeps_direct_dwd_reading_ads_untouched():
    """An ADS reading DWD tables directly is a real layering violation —
    the backfill must not rewrite its metadata to look compliant."""
    from wren_agent.nodes.validation import _backfill_warehouse_source_metadata
    from wren_agent.models.design import ADSDesign

    state, plan = _warehouse_state_with_plan()
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_x",
        source_dws_tables=["dwd_order_payment_cycle_acc"],
        dimensions=["stat_month"],
    )]
    plan.sql_artifacts.append(SQLArtifact(
        layer="ADS", target_table="ads_x", sql_type="DML_INSERT",
        sql="INSERT INTO ads_x SELECT * FROM dwd_order_payment_cycle_acc",
    ))

    _backfill_warehouse_source_metadata(state, plan)

    assert plan.ads_designs[0].source_dws_tables == [
        "dwd_order_payment_cycle_acc"
    ]  # untouched — still visibly violating


def test_backfill_noop_in_business_db_mode():
    from wren_agent.nodes.validation import _backfill_warehouse_source_metadata

    state, plan = _warehouse_state_with_plan()
    state["source_mode"] = "business_db"
    _backfill_warehouse_source_metadata(state, plan)

    assert plan.dws_designs[0].source_tables == []


def test_warehouse_ads_may_anchor_on_existing_warehouse_dws():
    """Warehouse mode: referencing an EXISTING explored DWS table (bare or
    db-qualified) is legitimate reuse, not a lineage violation."""
    from wren_agent.models.design import ADSDesign
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.validation import _validate_materialization_plan

    state, plan = _warehouse_state_with_plan()
    state["explorations"]["wh#dws"] = SchemaExploration(
        source_db_id="wh#dws",
        phase="light",
        tables=[TableSchema(
            name="dws_order_amount_collection_monthly",
            schema_name="db_dws",
            db_type="doris",
            fields=[FieldSchema(name="x", data_type="INT")],
        )],
    )
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_collection",
        source_dws_tables=["db_dws.dws_order_amount_collection_monthly"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "VARCHAR(7)"}],
        metrics=[{"name": "rate", "field_name": "rate", "expression": "rate"}],
    )]
    plan.sql_artifacts.append(SQLArtifact(
        layer="ADS", target_table="ads_collection", sql_type="DDL",
        sql="CREATE TABLE ads_collection (stat_month VARCHAR(7), rate DECIMAL(20,2))",
    ))
    plan.sql_artifacts.append(SQLArtifact(
        layer="ADS", target_table="ads_collection", sql_type="DML_INSERT",
        sql="INSERT INTO ads_collection SELECT stat_month, rate FROM db_dws.dws_order_amount_collection_monthly",
    ))

    results = _validate_materialization_plan(plan, state)
    lineage_failures = [
        f for r in results for f in r.failures
        if f["check"] == "ads_dws_lineage"
    ]
    assert lineage_failures == []


def test_warehouse_ads_can_source_this_plan_dws_tables():
    """ADS referencing THIS plan's DWS tables (db-qualified, not yet in
    exploration) must validate — their columns resolve from the plan DDLs via
    the plan landing. Task fd059c0a: the ADS call to
    _validate_warehouse_load_sql omitted `plan`, so every ADS built on the
    plan's own DWS tables failed warehouse_source_table_explored."""
    from wren_agent.models.design import ADSDesign
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.models.task import SourceDatabase
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t-ads", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [SourceDatabase(
        id="wh-1", name="wh", db_type="doris", host="h", port=9030,
        database="wren_dw", mcp_server_name="mcp_wh",
        connection_params={"layers": {"dws": ["db_dws"], "dwd": ["db_dwd"]}},
    )]
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd", phase="light",
            tables=[TableSchema(
                name="dwd_receivable_acc", schema_name="db_dwd", db_type="doris",
                fields=[FieldSchema(name="amount_receivable", data_type="DECIMAL")],
            )],
        ),
    }

    plan = DWDesignPlan(task_id="t-ads", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s", summary_table="dws_rental_monthly",
        source_dwd_table="dwd_receivable_acc",
        grain=["stat_month"], group_by_dimensions=["stat_month"],
        measures=[{
            "name": "daily_amount_receivable", "field_name": "daily_amount_receivable",
            "expression": "SUM(amount_receivable)", "type": "additive",
        }],
        fields=[
            {"name": "stat_month", "comment": "统计月份"},
            {"name": "daily_amount_receivable", "comment": "月度应收"},
        ],
    )]
    plan.sql_artifacts = [SQLArtifact(
        layer="DWS", target_table="dws_rental_monthly", sql_type="DDL",
        sql=("CREATE TABLE IF NOT EXISTS dws_rental_monthly ("
             "stat_month VARCHAR(7), daily_amount_receivable DECIMAL(20,2))"),
    )]
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_rental_monthly",
        source_dws_tables=["db_dws.dws_rental_monthly"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "VARCHAR(7)"},
                {"name": "未收金额", "type": "DECIMAL(20,2)"}],
        metrics=[{
            "name": "未收金额", "metric_type": "derived",
            "parent_metrics": ["daily_amount_receivable"],
            "expression": "daily_amount_receivable",
        }],
    )]
    plan.sql_artifacts.append(SQLArtifact(
        layer="ADS", target_table="ads_rental_monthly", sql_type="DDL",
        sql="CREATE TABLE IF NOT EXISTS ads_rental_monthly (stat_month VARCHAR(7), 未收金额 DECIMAL(20,2))",
    ))
    plan.sql_artifacts.append(SQLArtifact(
        layer="ADS", target_table="ads_rental_monthly", sql_type="DML_INSERT",
        sql=("INSERT INTO ads_rental_monthly (stat_month, 未收金额) "
             "SELECT stat_month, daily_amount_receivable FROM db_dws.dws_rental_monthly"),
    ))

    results = _validate_materialization_plan(plan, state)
    table_failures = [
        f for r in results for f in r.failures
        if f["check"] == "warehouse_source_table_explored"
    ]
    assert table_failures == []


def test_prefixed_dws_summary_table_still_resolves_ads_parents():
    """DWS summary_table may be declared db-qualified (LLM picked
    `dws_ecommerce_orders_sh.dws_...` in task f9b9a4bd) while ADS sources use
    bare names — parent/lineage lookups must resolve via bare keys, otherwise
    every derived metric fails with unknown parents."""
    from wren_agent.models.design import ADSDesign
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t-pref", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd", phase="light",
            tables=[TableSchema(
                name="dwd_receivable_acc", schema_name="db_dwd", db_type="doris",
                fields=[FieldSchema(name="amount_receivable", data_type="DECIMAL")],
            )],
        ),
    }

    plan = DWDesignPlan(task_id="t-pref", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s",
        summary_table="dws_ecommerce_orders_sh.dws_receivable_collection_unit_month",
        source_dwd_table="dwd_receivable_acc",
        grain=["stat_month"], group_by_dimensions=["stat_month"],
        measures=[{
            "name": "monthly_amount_receivable",
            "field_name": "monthly_amount_receivable",
            "expression": "SUM(amount_receivable)", "type": "additive",
        }],
        fields=[
            {"name": "stat_month", "comment": "统计月份"},
            {"name": "monthly_amount_receivable", "comment": "月度应收"},
        ],
    )]
    plan.ads_designs = [ADSDesign(
        application_name="a",
        ads_table="ads_receivable_collection_detail_month",
        source_dws_tables=["dws_receivable_collection_unit_month"],  # bare
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "VARCHAR(7)"},
                {"name": "月度支付率", "type": "DECIMAL(20,2)"}],
        metrics=[{
            "name": "月度支付率", "metric_type": "derived",
            "parent_metrics": ["monthly_amount_receivable"],
            "expression": "monthly_amount_receivable / monthly_amount_receivable",
        }],
    )]

    results = _validate_materialization_plan(plan, state)
    failures = [
        f for r in results for f in r.failures
        if f["check"] in ("derived_metric_parent_chain", "ads_dws_lineage")
    ]
    assert failures == []


def test_warehouse_target_db_prefix_must_be_known_layer_db():
    """Db-qualified DWS/ADS target names are legitimate (each layer is its own
    database), but the prefix must be a known layer db — landing, explored
    schema, or configured layer. An invented prefix would silently create the
    table in the wrong/nonexistent db at deploy (task f9b9a4bd)."""
    from wren_agent.models.design import ADSDesign
    from wren_agent.models.task import SourceDatabase
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t-pfx", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [SourceDatabase(
        id="wh-1", name="wh", db_type="doris", host="h", port=9030,
        database="wren_dw", mcp_server_name="mcp_wh",
        connection_params={
            "layers": {"dws": ["db_dws", "dws_ecommerce_orders_sh"],
                       "dwd": ["db_dwd"]},
            "landing": {"dws": "db_dws", "ads": "db_ads"},
        },
    )]

    def _pfx_plan(summary_table, ads_table):
        plan = DWDesignPlan(task_id="t-pfx", modeling_goal="goal")
        plan.dws_designs = [DWSDesign(
            summary_name="s", summary_table=summary_table,
            source_dwd_table="dwd_x", grain=["stat_month"],
            group_by_dimensions=["stat_month"],
            measures=[{"name": "m", "field_name": "m", "expression": "SUM(x)"}],
            fields=[{"name": "stat_month", "comment": "月"}, {"name": "m", "comment": "指标"}],
        )]
        plan.ads_designs = [ADSDesign(
            application_name="a", ads_table=ads_table,
            source_dws_tables=[summary_table.rsplit(".", 1)[-1]],
            dimensions=["stat_month"],
            fields=[{"name": "stat_month", "type": "VARCHAR(7)"}],
            metrics=[{"name": "m", "metric_type": "atomic", "expression": "m"}],
        )]
        return plan

    # Invalid prefix on DWS target → rejected
    bad = _pfx_plan("db_nonexistent.dws_x", "ads_x")
    results = _validate_materialization_plan(bad, state)
    assert any(
        f["check"] == "warehouse_target_db_prefix"
        for r in results for f in r.failures
    )

    # Valid prefix (configured layer db) → accepted; invalid ADS prefix rejected
    ok = _pfx_plan("dws_ecommerce_orders_sh.dws_x", "db_nonexistent.ads_y")
    results = _validate_materialization_plan(ok, state)
    assert not any(
        f["check"] == "warehouse_target_db_prefix" and "dws_x" in f["detail"]
        for r in results for f in r.failures
    )
    assert any(
        f["check"] == "warehouse_target_db_prefix" and "ads_y" in f["detail"]
        for r in results for f in r.failures
    )

    # Bare names (no prefix) are unaffected
    bare = _pfx_plan("dws_x", "ads_y")
    results = _validate_materialization_plan(bare, state)
    assert not any(
        f["check"] == "warehouse_target_db_prefix"
        for r in results for f in r.failures
    )


def test_group_by_must_cover_grain_but_may_add_descriptive_columns():
    """group_by_dimensions must COVER the grain; extra columns are legal as
    descriptive attributes (skill rule) — strict equality rejected sound
    designs (task 39e4327b: 13-dim group_by vs 5-col grain)."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    def _plan(group_by, grain):
        plan = DWDesignPlan(task_id="t-gb", modeling_goal="goal")
        plan.dws_designs = [DWSDesign(
            summary_name="s", summary_table="dws_x",
            source_dwd_table="dwd_x",
            grain=grain, group_by_dimensions=group_by,
            measures=[{"name": "m", "field_name": "m", "expression": "SUM(x)"}],
            fields=[{"name": c, "comment": "列"} for c in grain] +
                   [{"name": c, "comment": "列"} for c in group_by
                    if c not in grain],
        )]
        return plan

    # Extra descriptive columns (contract_code beside contract_id) are fine
    plan = _plan(
        ["snapshot_month", "contract_id", "contract_code", "contract_name",
         "building_id", "building_name"],
        ["snapshot_month", "contract_id", "building_id"],
    )
    results = _validate_materialization_plan(plan, {})
    assert not any(
        f["check"] == "group_by_matches_grain"
        for r in results for f in r.failures
    )

    # A group_by that MISSES a grain column still fails
    plan = _plan(["snapshot_month", "contract_id"], ["snapshot_month", "building_id"])
    results = _validate_materialization_plan(plan, {})
    assert any(
        f["check"] == "group_by_matches_grain" and "missing" in f["detail"]
        for r in results for f in r.failures
    )


def test_backfill_survives_prefixed_dws_and_bare_ads_refs():
    """design_revise's source-metadata backfill must not crash when the plan's
    DWS summary_table is db-qualified but the ADS DML references it bare.
    Task 77e9ae46: the double-key lookup passed `t in dws_targets` for the
    bare name, then `next()` matched full names only → StopIteration crashed
    design_revise and failed the whole task."""
    from wren_agent.models.design import ADSDesign
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.models.task import SourceDatabase
    from wren_agent.nodes.validation import _backfill_warehouse_source_metadata

    state = create_initial_state("t-bkf", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [SourceDatabase(
        id="wh-1", name="wh", db_type="doris", host="h", port=9030,
        database="wren_dw", mcp_server_name="mcp_wh",
        connection_params={
            "layers": {"dws": ["db_dws", "dws_ecommerce_orders_sh"],
                       "dwd": ["db_dwd"]},
            "landing": {"dws": "dws_ecommerce_orders_sh", "ads": "db_ads"},
        },
    )]
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd", phase="light",
            tables=[TableSchema(
                name="dwd_receivable_acc", schema_name="db_dwd", db_type="doris",
                fields=[FieldSchema(name="amount_receivable", data_type="DECIMAL")],
            )],
        ),
    }

    plan = DWDesignPlan(task_id="t-bkf", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s",
        summary_table="dws_ecommerce_orders_sh.dws_receivable_analysis_month",
        source_dwd_table="dwd_receivable_acc",
        source_tables=[],  # empty — the backfill must fill it from the DML
        grain=["stat_month"], group_by_dimensions=["stat_month"],
        measures=[{"name": "m", "field_name": "m", "expression": "SUM(x)"}],
        fields=[{"name": "stat_month", "comment": "月"}, {"name": "m", "comment": "指标"}],
    )]
    plan.ads_designs = [ADSDesign(
        application_name="a",
        ads_table="ads_receivable_analysis_month",
        source_dws_tables=["dws_receivable_analysis_month"],  # bare
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "VARCHAR(7)"}],
        metrics=[{"name": "m", "metric_type": "atomic", "expression": "m"}],
    )]
    # DWS DML (source for the backfill) + ADS DML referencing the plan DWS
    # table BARE — the crash scenario
    plan.sql_artifacts = [
        SQLArtifact(layer="DWS", target_table="dws_ecommerce_orders_sh.dws_receivable_analysis_month",
                    sql_type="DML_INSERT",
                    sql="INSERT INTO dws_ecommerce_orders_sh.dws_receivable_analysis_month (stat_month, m) "
                        "SELECT stat_month, m FROM db_dwd.dwd_receivable_acc"),
        SQLArtifact(layer="ADS", target_table="ads_receivable_analysis_month",
                    sql_type="DML_INSERT",
                    sql="INSERT INTO ads_receivable_analysis_month (stat_month, m) "
                        "SELECT stat_month, m FROM dws_receivable_analysis_month"),
    ]

    _backfill_warehouse_source_metadata(state, plan)  # must not raise
    assert plan.dws_designs[0].source_tables  # DWS source backfilled from DML
    assert plan.ads_designs[0].source_dws_tables  # ADS source synced, no crash


# ── DML column existence check (Level 1c) ───────────────────────


def _column_check_state(source_mode="business_db"):
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema

    state = create_initial_state("t-cols", "goal", [], {}, source_mode=source_mode)
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd",
            phase="light",
            tables=[TableSchema(
                name="dwd_fee_acc", schema_name="db_dwd", db_type="doris",
                fields=[
                    FieldSchema(name="org_id", data_type="VARCHAR"),
                    FieldSchema(name="cycle_start_date", data_type="DATE"),
                    FieldSchema(name="amount_receivable", data_type="DECIMAL"),
                ],
            )],
        ),
    }
    return state


def _dml_artifact(sql: str, layer="DWS") -> SQLArtifact:
    return SQLArtifact(layer=layer, target_table=f"{layer.lower()}_x", sql_type="DML_INSERT", sql=sql)


def test_dml_columns_exist_passes_for_real_columns():
    from wren_agent.nodes.validation import _validate_dml_columns

    result = _validate_dml_columns(_dml_artifact(
        "INSERT INTO dws_x SELECT t.org_id, SUM(t.amount_receivable) "
        "FROM db_dwd.dwd_fee_acc AS t GROUP BY t.org_id"
    ), _column_check_state())
    assert result is not None and result.passed is True


def test_dml_columns_exist_catches_hallucinated_column():
    from wren_agent.nodes.validation import _validate_dml_columns

    result = _validate_dml_columns(_dml_artifact(
        "INSERT INTO dws_x SELECT t.org_id, t.village_id "
        "FROM db_dwd.dwd_fee_acc t"
    ), _column_check_state())
    assert result is not None and result.passed is False
    assert "village_id" in result.failures[0]["detail"]
    assert "amount_receivable" in result.failures[0]["detail"]  # available list


def test_dml_columns_exist_covers_warehouse_ads():
    """Warehouse ADS load SQL is gated by _validate_warehouse_load_sql."""
    from wren_agent.nodes.validation import _validate_warehouse_load_sql

    state = _column_check_state(source_mode="warehouse")
    artifacts = [_dml_artifact(
        "INSERT INTO ads_x SELECT t.org_id, t.village_id FROM db_dwd.dwd_fee_acc t",
        layer="ADS",
    )]
    failures = _validate_warehouse_load_sql("ads_x", artifacts, state)
    assert any(f["check"] == "warehouse_source_columns_exist" for f in failures)
    assert any("village_id" in f["detail"] for f in failures)
    assert any("Available columns:" in f["detail"] for f in failures)


def test_dml_columns_exist_skips_warehouse_dws():
    from wren_agent.nodes.validation import _validate_dml_columns

    result = _validate_dml_columns(_dml_artifact(
        "INSERT INTO dws_x SELECT t.village_id FROM db_dwd.dwd_fee_acc t"
    ), _column_check_state(source_mode="warehouse"))
    assert result is None  # handled by _validate_warehouse_load_sql


def test_dml_columns_exist_skips_warehouse_ads_level1c():
    from wren_agent.nodes.validation import _validate_dml_columns

    result = _validate_dml_columns(_dml_artifact(
        "INSERT INTO ads_x SELECT t.village_id FROM db_dwd.dwd_fee_acc t",
        layer="ADS",
    ), _column_check_state(source_mode="warehouse"))
    assert result is None  # handled by _validate_warehouse_load_sql


def test_dml_columns_exist_skips_unexplored_tables():
    from wren_agent.nodes.validation import _validate_dml_columns

    result = _validate_dml_columns(_dml_artifact(
        "INSERT INTO dws_x SELECT t.anything FROM not_explored_table t"
    ), _column_check_state())
    assert result is not None and result.passed is True  # best-effort skip


def test_dml_columns_exist_catches_bare_dim_source_columns():
    """Regression de018b42: DIM INSERT SELECT id, code FROM ods_x with no
    alias must still fail when `code` is absent from the explored ODS table."""
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.validation import _validate_dml_columns

    state = create_initial_state("t-dim", "goal", [], {}, source_mode="business_db")
    state["explorations"] = {
        "src#ods": SchemaExploration(
            source_db_id="src#ods",
            phase="light",
            tables=[TableSchema(
                name="ods_basics_floor", schema_name="ods_gz", db_type="doris",
                fields=[
                    FieldSchema(name="id", data_type="BIGINT"),
                    FieldSchema(name="floor_code", data_type="VARCHAR"),
                    FieldSchema(name="floor_name", data_type="VARCHAR"),
                    FieldSchema(name="store_id", data_type="VARCHAR"),
                ],
            )],
        ),
    }
    result = _validate_dml_columns(SQLArtifact(
        layer="DIM", target_table="dim_floor", sql_type="DML_INSERT",
        sql=(
            "INSERT INTO dim_floor (floor_id, dim_code, dim_name, store_id) "
            "SELECT id, code, name, store_id "
            "FROM ods_gz.ods_basics_floor"
        ),
    ), state)
    assert result is not None and result.passed is False
    detail = " ".join(f["detail"] for f in result.failures)
    assert "code" in detail
    assert "name" in detail
    assert "floor_code" in detail


def test_dml_columns_exist_accepts_bare_columns_that_exist():
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.validation import _validate_dml_columns

    state = create_initial_state("t-dim", "goal", [], {}, source_mode="business_db")
    state["explorations"] = {
        "src#ods": SchemaExploration(
            source_db_id="src#ods",
            phase="light",
            tables=[TableSchema(
                name="ods_operate_customer", schema_name="ods_gz",
                db_type="doris",
                fields=[
                    FieldSchema(name="id", data_type="BIGINT"),
                    FieldSchema(name="tenant_name", data_type="VARCHAR"),
                    FieldSchema(name="tenant_code", data_type="VARCHAR"),
                ],
            )],
        ),
    }
    result = _validate_dml_columns(SQLArtifact(
        layer="DIM", target_table="dim_tenant", sql_type="DML_INSERT",
        sql=(
            "INSERT INTO dim_tenant (tenant_id, tenant_code, tenant_name) "
            "SELECT id, tenant_code, tenant_name "
            "FROM ods_gz.ods_operate_customer"
        ),
    ), state)
    assert result is not None and result.passed is True
