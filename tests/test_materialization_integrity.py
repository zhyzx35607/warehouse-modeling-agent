"""Regression tests for executable DWS/ADS materialization and truthful reports."""

from __future__ import annotations

import pytest

from wren_agent.models.design import (
    ADSDesign,
    DIMDesign,
    DWDDesign,
    DWSDesign,
    DWDesignPlan,
    SQLArtifact,
)
from wren_agent.models.task import (
    ClarificationItem,
    ClarificationStatus,
    ExecutionResult,
    TaskStatus,
)
from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.state import create_initial_state


def _valid_materialized_plan() -> DWDesignPlan:
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="daily collection",
        summary_table="dws_collection_daily",
        source_dwd_table="fct_payment",
        source_tables=["fct_payment"],
        fields=[
            {"name": "stat_date", "type": "DATE"},
            {"name": "org_id", "type": "VARCHAR(255)"},
        ],
        measures=[{"name": "due_amount", "expression": "SUM(due_amount)"}],
        group_by_dimensions=["stat_date", "org_id"],
        grain=["stat_date", "org_id"],
        requirement_coverage=[{
            "requirement": "daily organization collection",
            "status": "supported",
            "evidence": ["stat_date", "org_id", "due_amount"],
            "gap_reason": "",
        }],
    )]
    plan.ads_designs = [ADSDesign(
        application_name="collection dashboard",
        ads_table="ads_collection",
        source_dws_tables=["dws_collection_daily"],
        dimensions=["stat_date", "org_id"],
        fields=[
            {"name": "stat_date", "type": "DATE"},
            {"name": "org_id", "type": "VARCHAR(255)"},
            {"name": "collection_rate", "type": "DECIMAL(20,2)"},
        ],
        metrics=[{
            "name": "collection rate",
            "field_name": "collection_rate",
            "expression": "collection_rate",
            "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "daily organization collection rate",
            "status": "supported",
            "evidence": ["stat_date", "org_id", "collection_rate"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_collection_daily", sql_type="DDL",
            sql="CREATE TABLE dws_collection_daily (stat_date DATE, org_id VARCHAR(255), due_amount DECIMAL(20,2)) UNIQUE KEY(stat_date, org_id)",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_collection_daily", sql_type="DML_INSERT",
            sql="INSERT INTO dws_collection_daily (stat_date, org_id, due_amount) SELECT CURRENT_DATE, org_id, SUM(due_amount) FROM fct_payment GROUP BY org_id",
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_collection", sql_type="DDL",
            sql="CREATE TABLE ads_collection (stat_date DATE, org_id VARCHAR(255), collection_rate DECIMAL(20,2))",
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_collection", sql_type="DML_INSERT",
            sql="INSERT INTO ads_collection (stat_date, org_id, collection_rate) SELECT stat_date, org_id, collection_rate FROM dws_collection_daily",
        ),
    ]
    return plan


def test_llm_parsers_keep_load_sql_and_physical_dimensions():
    from wren_agent.nodes.design_layers import (
        _parse_ads_llm_result,
        _parse_dwd_dws_llm_result,
    )

    _, dws_designs, dws_artifacts = _parse_dwd_dws_llm_result({
        "dws_designs": [{
            "summary_name": "daily",
            "summary_table": "dws_x_daily",
            "source_dwd_table": "fct_x",
            "source_tables": ["db_dwd.fct_x"],
            "fields": [{"name": "stat_date", "type": "DATE"}],
            "measures": [{"name": "amount", "expression": "SUM(amount)"}],
            "group_by_dimensions": ["stat_date"],
            "grain": ["stat_date"],
            "requirement_coverage": [{
                "requirement": "daily amount",
                "status": "supported",
                "evidence": ["stat_date", "amount"],
                "gap_reason": "",
            }],
            "ddl": "CREATE TABLE dws_x_daily (stat_date DATE, amount DECIMAL(20,2)) UNIQUE KEY(stat_date)",
            "load_sql": "INSERT INTO dws_x_daily SELECT DATE(dt), SUM(amount) FROM db_dwd.fct_x GROUP BY DATE(dt)",
        }]
    })
    assert dws_designs[0].source_tables == ["db_dwd.fct_x"]
    assert {a.sql_type for a in dws_artifacts} == {"DDL", "DML_INSERT"}

    ads_designs, ads_artifacts, skipped, _ = _parse_ads_llm_result({
        "designs": [{
            "application_name": "dashboard",
            "ads_table": "ads_x",
            "source_dws_tables": ["dws_x_daily"],
            "dimensions": ["stat_date"],
            "fields": [
                {"name": "stat_date", "type": "DATE"},
                {"name": "amount", "type": "DECIMAL(20,2)"},
            ],
            "metrics": [{
                "name": "amount",
                "field_name": "amount",
                "expression": "amount",
                "metric_type": "atomic",
                "parent_metrics": [],
            }],
            "requirement_coverage": [{
                "requirement": "daily amount",
                "status": "supported",
                "evidence": ["stat_date", "amount"],
                "gap_reason": "",
            }],
            "ddl": "CREATE TABLE ads_x (stat_date DATE, amount DECIMAL(20,2))",
            "load_sql": "INSERT INTO ads_x SELECT stat_date, amount FROM dws_x_daily",
        }]
    })
    assert skipped is False
    assert ads_designs[0].dimensions == ["stat_date"]
    assert {a.sql_type for a in ads_artifacts} == {"DDL", "DML_INSERT"}


def test_dim_parser_keeps_load_sql():
    """Regression 9c52047f: DIM designs must emit INSERT, not only CREATE TABLE."""
    from wren_agent.nodes.design_layers import _parse_dim_llm_result

    designs, artifacts = _parse_dim_llm_result({
        "designs": [{
            "dim_name": "dim_tenant",
            "dim_table": "dim_tenant",
            "source": "from_source_table",
            "source_table": "ods_operate_customer",
            "fields": [{"name": "dim_name", "source_type": "VARCHAR", "comment": "名称"}],
            "natural_key": ["id"],
            "surrogate_key": "dim_tenant_id",
            "ddl": (
                "CREATE TABLE IF NOT EXISTS dim_tenant ("
                "dim_tenant_id BIGINT NOT NULL COMMENT '代理键', "
                "dim_name VARCHAR(255) COMMENT '名称', "
                "effective_date DATE COMMENT '生效', "
                "expiration_date DATE COMMENT '失效', "
                "is_current TINYINT DEFAULT 1 COMMENT '当前'"
                ") DUPLICATE KEY(dim_tenant_id) COMMENT '维度表: 客户'"
            ),
            "load_sql": (
                "INSERT INTO dim_tenant "
                "(dim_tenant_id, dim_name, effective_date, expiration_date, is_current) "
                "SELECT id, name, CURRENT_DATE(), CAST('9999-12-31' AS DATE), 1 "
                "FROM ods_gz_test.ods_operate_customer"
            ),
        }]
    })
    assert designs[0].dim_table == "dim_tenant"
    assert {a.sql_type for a in artifacts} == {"DDL", "DML_INSERT"}
    insert = next(a.sql for a in artifacts if a.sql_type == "DML_INSERT")
    assert "INSERT INTO dim_tenant" in insert
    assert "ods_operate_customer" in insert


def test_dwd_parser_keeps_load_sql():
    """Regression 9c52047f: DWD facts must emit INSERT from ODS, not only DDL."""
    from wren_agent.nodes.design_layers import _parse_dwd_dws_llm_result

    dwd_designs, _, artifacts = _parse_dwd_dws_llm_result({
        "dwd_designs": [{
            "fact_name": "出租合同",
            "fact_table": "dwd_operate_customer_contract",
            "business_process": "ods_operate_customer_contract",
            "source_ods_tables": ["ods_operate_customer_contract"],
            "fields": [{"name": "contract_id", "source_column": "id", "type": "VARCHAR(255)"}],
            "measures": [],
            "dimension_refs": ["dim_tenant"],
            "grain": ["contract_id"],
            "ddl": (
                "CREATE TABLE IF NOT EXISTS dwd_operate_customer_contract ("
                "contract_id VARCHAR(255) COMMENT '合同'"
                ") DUPLICATE KEY(contract_id) COMMENT '明细事实层: 合同'"
            ),
            "load_sql": (
                "INSERT INTO dwd_operate_customer_contract (contract_id) "
                "SELECT id FROM ods_gz_test.ods_operate_customer_contract"
            ),
        }]
    })
    dwd_arts = [a for a in artifacts if a.layer == "DWD"]
    assert dwd_designs[0].fact_table == "dwd_operate_customer_contract"
    assert {a.sql_type for a in dwd_arts} == {"DDL", "DML_INSERT"}


def test_existing_dim_does_not_require_load_sql():
    from wren_agent.nodes.design_layers import _parse_dim_llm_result

    designs, artifacts = _parse_dim_llm_result({
        "designs": [{
            "dim_name": "dim_org",
            "dim_table": "dim_org",
            "source": "from_existing_dim",
            "existing_dim_ref": "dim_org",
            "fields": [],
            "natural_key": ["org_id"],
            "ddl": None,
        }]
    })
    assert designs[0].source == "from_existing_dim"
    assert artifacts == []


def test_core_model_rejects_ddl_only_dim_and_dwd():
    """Regression 9c52047f: harness must block DIM/DWD CREATE-only designs."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = DWDesignPlan(task_id="t", modeling_goal="core")
    plan.dim_designs = [DIMDesign(
        dim_name="dim_tenant", dim_table="dim_tenant",
        source="from_source_table", source_table="ods_operate_customer",
        fields=[{"name": "dim_name"}],
        natural_key=["id"], surrogate_key="dim_tenant_id",
    )]
    plan.dwd_designs = [DWDDesign(
        fact_name="合同", fact_table="dwd_operate_customer_contract",
        source_ods_tables=["ods_operate_customer_contract"],
        fields=[{"name": "contract_id"}], grain=["contract_id"],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DIM", target_table="dim_tenant", sql_type="DDL",
            sql="CREATE TABLE dim_tenant (dim_tenant_id BIGINT)",
        ),
        SQLArtifact(
            layer="DWD", target_table="dwd_operate_customer_contract", sql_type="DDL",
            sql="CREATE TABLE dwd_operate_customer_contract (contract_id VARCHAR(255))",
        ),
    ]
    state = create_initial_state(
        "t", "goal", [], {}, source_mode="business_db",
        business_task_type="core_model",
        landing={"ods": "ods_gz_test", "dim": "dim_gz_test", "dwd": "dwd_gz_test"},
    )
    failures = [
        f for result in _validate_materialization_plan(plan, state)
        for f in result.failures
    ]
    checks = {f["check"] for f in failures}
    assert "materialization_sql_complete" in checks
    details = " ".join(str(f.get("detail", "")) for f in failures)
    assert "dim_tenant" in details
    assert "dwd_operate_customer_contract" in details


def test_existing_dim_skips_materialization_sql_complete():
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = DWDesignPlan(task_id="t", modeling_goal="core")
    plan.dim_designs = [DIMDesign(
        dim_name="dim_org", dim_table="dim_org",
        source="from_existing_dim", existing_dim_ref="dim_org",
        natural_key=["org_id"], registered=True,
    )]
    state = create_initial_state(
        "t", "goal", [], {}, business_task_type="core_model",
    )
    failures = [
        f for result in _validate_materialization_plan(plan, state)
        for f in result.failures
    ]
    assert not any(f["check"] == "materialization_sql_complete" for f in failures)


def test_dim_bootstrap_load_sql_selects_ods_source():
    from wren_agent.nodes.design_layers import _dim_bootstrap_load_sql

    dim = DIMDesign(
        dim_name="dim_tenant", dim_table="dim_tenant",
        source="from_source_table", source_table="operate_customer_tenant",
        fields=[{"name": "dim_name", "comment": "名称"}, {"name": "status"}],
        natural_key=["id"], surrogate_key="dim_tenant_id",
    )
    sql = _dim_bootstrap_load_sql(dim, ods_database="ods_gz_test")
    assert "INSERT INTO dim_tenant" in sql
    assert "dim_tenant_id" in sql
    assert "CURRENT_DATE()" in sql
    assert "ods_gz_test.ods_operate_customer" in sql


def test_materialization_invariants_reject_ddl_only_daily_design():
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = _valid_materialized_plan()
    plan.sql_artifacts = [
        a for a in plan.sql_artifacts
        if not (a.layer == "DWS" and a.sql_type == "DML_INSERT")
    ]
    plan.dws_designs[0].grain = ["org_id"]
    state = create_initial_state("t", "goal", [], {})

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert any(f["check"] == "materialization_sql_complete" for f in failures)
    assert any(f["check"] == "daily_time_grain" for f in failures)


def test_materialization_invariants_reject_incomplete_wide_table_contract():
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = _valid_materialized_plan()
    dws = plan.dws_designs[0]
    dws.group_by_dimensions = ["stat_date"]
    dws.measures.append({
        "name": "rentable_area",
        "expression": "MAX(rentable_area)",
    })
    dws.requirement_coverage.append({
        "requirement": "unit grain",
        "status": "partial",
        "evidence": [],
        "gap_reason": "unit_id unavailable",
    })

    ads = plan.ads_designs[0]
    ads.metrics[0] = {
        "name": "collection rate",
        "field_name": "collection_rate",
        "expression": "paid_amount / due_amount",
        "metric_type": "derived",
        "parent_metrics": [],
    }

    results = _validate_materialization_plan(
        plan, create_initial_state("t", "goal", [], {})
    )
    failures = [failure for result in results for failure in result.failures]
    checks = {failure["check"] for failure in failures}

    assert "group_by_matches_grain" in checks
    assert "dws_fields_materialized_in_ddl" in checks
    assert "dws_fields_materialized_in_load_sql" in checks
    assert "requirement_not_fully_supported" in checks
    assert "derived_metric_parent_chain" in checks


def test_requirement_coverage_allows_explicitly_accepted_partial_support():
    from wren_agent.nodes.validation import _coverage_failures

    design = DWSDesign(
        summary_name="partial",
        summary_table="dws_partial",
        source_dwd_table="dwd_x",
        requirement_coverage=[{
            "requirement": "unit daily grain",
            "status": "partial",
            "evidence": ["stat_date", "building_id"],
            "gap_reason": "unit_id is unavailable and the user accepted shop-only output",
        }],
    )

    assert _coverage_failures(design, design.summary_table)
    assert _coverage_failures(
        design, design.summary_table, allow_incomplete=True
    ) == []


def test_ads_dimensions_accept_materialized_in_ddl_only():
    """Regression 6ba95453: ADS dimensions count as materialized when the
    DDL physically contains them, even if the fields list omits them."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_room_analysis",
        source_dws_tables=["dws_room_monthly"],
        dimensions=["stat_month", "basics_room_id"],
        fields=[{"name": "monthly_amount", "type": "DECIMAL(20,2)"}],
        metrics=[{
            "name": "应收金额", "field_name": "monthly_amount",
            "expression": "monthly_amount", "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["monthly_amount"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_room_analysis", sql_type="DDL",
            sql=("CREATE TABLE ads_room_analysis (stat_month DATE, "
                 "basics_room_id VARCHAR(255), monthly_amount DECIMAL(20,2))"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_room_analysis", sql_type="DML_INSERT",
            sql=("INSERT INTO ads_room_analysis (stat_month, basics_room_id, monthly_amount) "
                 "SELECT stat_month, basics_room_id, monthly_amount FROM dws_room_monthly"),
        ),
    ]

    results = _validate_materialization_plan(plan, create_initial_state("t", "goal", [], {}))
    failures = [f for result in results for f in result.failures]
    assert not any(f["check"] == "ads_dimensions_materialized" for f in failures)


def test_coverage_is_plan_level_sibling_table_covers_requirement():
    """Regression f3511018: an org-level table marking '按商品分析' unsupported
    is fine when the product-level table in the same plan covers it — coverage
    is a plan-level order, not per-table."""
    from wren_agent.nodes.validation import _coverage_failures_plan

    org = DWSDesign(
        summary_name="org monthly", summary_table="dws_org_monthly",
        source_dwd_table="dwd_x",
        requirement_coverage=[
            {
                "requirement": "按单位分析",
                "status": "supported",
                "evidence": ["org_id", "monthly_amount"],
                "gap_reason": "",
            },
            {
                "requirement": "按商品分析",
                "status": "unsupported",
                "gap_reason": "本表按单位汇总，商品分析见商品月度表",
            },
        ],
    )
    product = DWSDesign(
        summary_name="product monthly", summary_table="dws_room_monthly",
        source_dwd_table="dwd_x",
        requirement_coverage=[
            {
                "requirement": "按商品分析",
                "status": "supported",
                "evidence": ["basics_room_id", "monthly_amount"],
                "gap_reason": "",
            },
        ],
    )
    assert _coverage_failures_plan([org, product]) == []


def test_coverage_plan_level_uncovered_requirement_still_fails():
    from wren_agent.nodes.validation import _coverage_failures_plan

    org = DWSDesign(
        summary_name="org monthly", summary_table="dws_org_monthly",
        source_dwd_table="dwd_x",
        requirement_coverage=[{
            "requirement": "按商品分析",
            "status": "unsupported",
            "gap_reason": "源表无商品键",
        }],
    )
    product = DWSDesign(
        summary_name="product monthly", summary_table="dws_room_monthly",
        source_dwd_table="dwd_x",
        requirement_coverage=[{
            "requirement": "按商品分析",
            "status": "unsupported",
            "gap_reason": "源表无商品键",
        }],
    )
    failures = _coverage_failures_plan([org, product])
    assert any(f["check"] == "requirement_not_fully_supported" for f in failures)


def test_coverage_plan_level_derived_metric_deferred_to_ads():
    """A DWS table may mark a derived ratio as partial when the ADS layer
    computes it — the ADS supported entry satisfies the plan."""
    from wren_agent.nodes.validation import _coverage_failures_plan

    dws = DWSDesign(
        summary_name="daily", summary_table="dws_rental_daily",
        source_dwd_table="dwd_x",
        requirement_coverage=[{
            "requirement": "租金支付率",
            "status": "partial",
            "gap_reason": "比值类派生指标由 ADS 层基于本表原子指标计算",
        }],
    )
    ads = ADSDesign(
        application_name="a", ads_table="ads_rate",
        source_dws_tables=["dws_rental_daily"],
        dimensions=["stat_date"],
        fields=[{"name": "stat_date", "type": "DATE"}],
        metrics=[{
            "name": "支付率", "field_name": "collection_rate",
            "expression": "paid / receivable", "metric_type": "derived",
            "parent_metrics": ["daily_real_rent", "daily_receivable_rent"],
        }],
        requirement_coverage=[{
            "requirement": "租金支付率",
            "status": "supported",
            "evidence": ["collection_rate"],
            "gap_reason": "",
        }],
    )
    assert _coverage_failures_plan([dws, ads]) == []


def _warehouse_state_with_payment_schema():
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "warehouse#dwd": SchemaExploration(
            source_db_id="warehouse#dwd",
            tables=[TableSchema(
                name="dwd_order_payment_cycle_acc",
                schema_name="db_dwd",
                db_type="doris",
                fields=[
                    FieldSchema(name="cycle_start_date", data_type="DATE"),
                    FieldSchema(name="amount_receivable", data_type="DECIMAL(20,2)"),
                    FieldSchema(name="cumulative_paid_amount", data_type="DECIMAL(20,2)"),
                    FieldSchema(name="order_estate_id", data_type="BIGINT"),
                ],
            )],
        ),
    }
    return state


def test_warehouse_invariants_reject_hallucinated_qualified_source_column():
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = _valid_materialized_plan()
    plan.dws_designs[0].source_dwd_table = "dwd_order_payment_cycle_acc"
    plan.dws_designs[0].source_tables = ["db_dwd.dwd_order_payment_cycle_acc"]
    dml = next(a for a in plan.sql_artifacts if a.layer == "DWS" and a.sql_type == "DML_INSERT")
    dml.sql = (
        "INSERT INTO dws_collection_daily "
        "SELECT dwd.cycle_start_date, dwd.basics_village_id, SUM(dwd.amount_receivable) "
        "FROM db_dwd.dwd_order_payment_cycle_acc AS dwd "
        "GROUP BY dwd.cycle_start_date, dwd.basics_village_id"
    )

    results = _validate_materialization_plan(plan, _warehouse_state_with_payment_schema())
    failures = [f for result in results for f in result.failures]

    assert any(f["check"] == "warehouse_source_columns_exist" for f in failures)
    assert any("dwd.basics_village_id" in f["detail"] for f in failures)
    assert any("Available columns:" in f["detail"] for f in failures)


def test_warehouse_invariants_reject_bare_hallucinated_source_column():
    """Regression b131f22e: bare `group_id` must fail harness, not only Doris."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    plan = _valid_materialized_plan()
    plan.dws_designs[0].source_dwd_table = "dwd_order_payment_cycle_acc"
    plan.dws_designs[0].source_tables = [
        "dwd_ecommerce_orders_sh.dwd_order_payment_cycle_acc"
    ]
    state = _warehouse_state_with_payment_schema()
    # Match the real warehouse schema key used by the failing task.
    state["explorations"]["warehouse#dwd"].tables[0].schema_name = "dwd_ecommerce_orders_sh"
    dml = next(a for a in plan.sql_artifacts if a.layer == "DWS" and a.sql_type == "DML_INSERT")
    dml.sql = (
        "INSERT INTO dws_ecommerce_orders_sh.dws_order_payment_daily "
        "(stat_date, contract_id, group_id, daily_amount_receivable) "
        "SELECT DATE(cycle_start_date) AS stat_date, "
        "CAST(order_estate_id AS STRING) AS contract_id, "
        "CAST(group_id AS STRING) AS group_id, "
        "SUM(amount_receivable) AS daily_amount_receivable "
        "FROM dwd_ecommerce_orders_sh.dwd_order_payment_cycle_acc "
        "WHERE cycle_start_date IS NOT NULL "
        "GROUP BY DATE(cycle_start_date), order_estate_id, group_id"
    )

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]

    assert any(f["check"] == "warehouse_source_columns_exist" for f in failures)
    assert any("group_id" in f["detail"] for f in failures)
    assert any("Available columns:" in f["detail"] for f in failures)


def test_warehouse_ads_parents_resolve_against_existing_dws_columns():
    """Regression 15ec38e7: an ADS anchored on an EXISTING warehouse DWS table
    (legal per 1f49d2d7) must resolve its derived-metric parent_metrics
    against that table's explored columns — not just this plan's own DWS
    designs' measures."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dws": SchemaExploration(source_db_id="wh#dws", tables=[
            TableSchema(
                name="dws_payment_collection_monthly", schema_name="db_dws",
                db_type="doris",
                fields=[
                    FieldSchema(name="stat_month", data_type="DATE"),
                    FieldSchema(name="monthly_contract_amount", data_type="DECIMAL(20,2)"),
                    FieldSchema(name="monthly_cumulative_paid_rent", data_type="DECIMAL(20,2)"),
                ],
            ),
        ]),
    }

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_collection_rate",
        source_dws_tables=["db_dws.dws_payment_collection_monthly"],
        dimensions=["stat_month"],
        fields=[
            {"name": "stat_month", "type": "DATE"},
            {"name": "collection_rate", "type": "DECIMAL(20,4)"},
        ],
        metrics=[{
            "name": "租金支付率", "field_name": "collection_rate",
            "expression": "monthly_cumulative_paid_rent / monthly_contract_amount",
            "metric_type": "derived",
            "parent_metrics": ["monthly_cumulative_paid_rent", "monthly_contract_amount"],
        }],
        requirement_coverage=[{
            "requirement": "租金支付率",
            "status": "supported",
            "evidence": ["collection_rate"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_collection_rate", sql_type="DDL",
            sql="CREATE TABLE ads_collection_rate (stat_month DATE, collection_rate DECIMAL(20,4))",
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_collection_rate", sql_type="DML_INSERT",
            sql="INSERT INTO ads_collection_rate (stat_month, collection_rate) "
                "SELECT stat_month, monthly_cumulative_paid_rent / monthly_contract_amount "
                "FROM db_dws.dws_payment_collection_monthly",
        ),
    ]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert not any(f["check"] == "derived_metric_parent_chain" for f in failures)
    assert not any(f["check"] == "ads_dws_lineage" for f in failures)


def test_warehouse_invariants_require_aliases_for_multiple_sources():
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = _warehouse_state_with_payment_schema()
    state["explorations"]["warehouse#dim"] = SchemaExploration(
        source_db_id="warehouse#dim",
        tables=[TableSchema(
            name="dim_house_purpose", schema_name="db_dim", db_type="doris",
            fields=[FieldSchema(name="house_purpose_id", data_type="BIGINT")],
        )],
    )
    plan = _valid_materialized_plan()
    plan.dws_designs[0].source_dwd_table = "dwd_order_payment_cycle_acc"
    plan.dws_designs[0].source_tables = [
        "db_dwd.dwd_order_payment_cycle_acc",
        "db_dim.dim_house_purpose",
    ]
    dml = next(a for a in plan.sql_artifacts if a.layer == "DWS" and a.sql_type == "DML_INSERT")
    dml.sql = (
        "INSERT INTO dws_collection_daily "
        "SELECT dwd.cycle_start_date, SUM(dwd.amount_receivable) "
        "FROM db_dwd.dwd_order_payment_cycle_acc AS dwd "
        "JOIN db_dim.dim_house_purpose ON house_purpose_id = dwd.order_estate_id "
        "GROUP BY dwd.cycle_start_date"
    )

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]

    assert any(f["check"] == "warehouse_source_alias_required" for f in failures)


def test_insert_target_columns_fallback_column_less_select():
    """Regression a23e1599: a column-less `INSERT INTO t SELECT ...` DML is
    valid skill-compliant Doris SQL — the harness must derive the target
    columns from the SELECT aliases instead of reporting every declared
    field/measure as missing from the INSERT target columns."""
    from wren_agent.nodes.validation import _insert_target_column_names

    sql = (
        "INSERT INTO dws_order_revenue_daily SELECT\n"
        "  DATE_FORMAT(t1.contract_start_date, '%Y-%m-%d') AS stat_date,\n"
        "  CAST(t1.store_id AS VARCHAR(255)) AS store_id,\n"
        "  COALESCE(d3.house_name, '') AS house_name,\n"
        "  SUM(COALESCE(t1.contract_amount_first_year, 0) / 365) AS daily_receivable_rent,\n"
        "  SUM(COALESCE(t1.cumulative_paid_deposit, 0) / 365) AS daily_real_deposit\n"
        "FROM dwd_ecommerce_orders_BJ.fct_order_payment_contract_acc t1\n"
        "LEFT JOIN dim_ecommerce_shop_bj.dim_order_shop d3 "
        "ON t1.store_id = d3.store_id\n"
        "GROUP BY DATE_FORMAT(t1.contract_start_date, '%Y-%m-%d'), "
        "t1.store_id, d3.house_name"
    )
    assert _insert_target_column_names(sql) == {
        "stat_date", "store_id", "house_name",
        "daily_receivable_rent", "daily_real_deposit",
    }


def test_insert_target_columns_fallback_bare_identifiers():
    """Column-less SELECT with bare passthrough columns (no AS) maps them
    positionally to target columns — e.g. a monthly DWS rolling up a daily
    DWS: `store_id,` in the SELECT list is a target column too."""
    from wren_agent.nodes.validation import _insert_target_column_names

    sql = (
        "INSERT INTO dws_order_revenue_monthly SELECT\n"
        "  SUBSTRING(stat_date, 1, 4) AS stat_year,\n"
        "  SUBSTRING(stat_date, 6, 2) AS stat_month,\n"
        "  store_id,\n"
        "  org_id,\n"
        "  SUM(daily_receivable_rent) AS monthly_receivable_rent\n"
        "FROM wren_dw.dws_order_revenue_daily\n"
        "GROUP BY SUBSTRING(stat_date, 1, 4), SUBSTRING(stat_date, 6, 2), "
        "store_id, org_id"
    )
    assert _insert_target_column_names(sql) == {
        "stat_year", "stat_month", "store_id", "org_id",
        "monthly_receivable_rent",
    }


def test_insert_target_columns_explicit_list_preferred():
    """The explicit `INSERT INTO t (col, ...)` form keeps its behavior."""
    from wren_agent.nodes.validation import _insert_target_column_names

    assert _insert_target_column_names(
        "INSERT INTO t (a, b, c) SELECT x, y, z FROM src"
    ) == {"a", "b", "c"}
    # OVERWRITE variant of the column-less form
    assert _insert_target_column_names(
        "INSERT INTO t OVERWRITE SELECT x AS a, y AS b FROM src"
    ) == {"a", "b"}
    # Not an INSERT at all
    assert _insert_target_column_names("SELECT * FROM t") == set()


def test_warehouse_sibling_dws_validated_against_plan_ddl():
    """Regression a23e1599 (P1-4): a DWS built on a sibling DWS of the same
    plan must not fail as 'not explored' / 'unqualified' — its columns are
    resolved from the plan's own DDL and refs are qualified with the landing
    database."""
    from wren_agent.nodes.validation import (
        _rewrite_warehouse_plan_table_refs,
        _validate_materialization_plan,
    )

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dwd": "db_dwd", "dim": "db_dim", "dws": "db_dws", "ads": "db_ads",
            },
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {"warehouse#dwd": SchemaExploration(
        source_db_id="warehouse#dwd",
        tables=[TableSchema(
            name="fct_payment", schema_name="db_dwd", db_type="doris",
            fields=[FieldSchema(name="amount", data_type="DECIMAL(20,2)")],
        )],
    )}

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [
        DWSDesign(
            summary_name="daily", summary_table="dws_rental_daily",
            source_dwd_table="db_dwd.fct_payment",
            source_tables=["db_dwd.fct_payment"],
            fields=[
                {"name": "stat_date", "type": "DATE"},
                {"name": "org_id", "type": "VARCHAR(255)"},
            ],
            measures=[{"name": "daily_amt", "expression": "SUM(amount)"}],
            group_by_dimensions=["stat_date", "org_id"],
            grain=["stat_date", "org_id"],
            requirement_coverage=[{
                "requirement": "daily amount by org",
                "status": "supported",
                "evidence": ["stat_date", "org_id", "daily_amt"],
                "gap_reason": "",
            }],
        ),
        DWSDesign(
            summary_name="monthly", summary_table="dws_rental_monthly",
            source_dwd_table="dws_rental_daily",
            source_tables=["dws_rental_daily"],
            fields=[
                {"name": "stat_month", "type": "DATE"},
                {"name": "org_id", "type": "VARCHAR(255)"},
            ],
            measures=[{"name": "monthly_amt", "expression": "SUM(daily_amt)"}],
            group_by_dimensions=["stat_month", "org_id"],
            grain=["stat_month", "org_id"],
            requirement_coverage=[{
                "requirement": "monthly amount by org",
                "status": "supported",
                "evidence": ["stat_month", "org_id", "monthly_amt"],
                "gap_reason": "",
            }],
        ),
    ]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_rental_daily", sql_type="DDL",
            sql=("CREATE TABLE dws_rental_daily (stat_date DATE, org_id VARCHAR(255), "
                 "daily_amt DECIMAL(20,2)) UNIQUE KEY(stat_date, org_id)"),
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_daily", sql_type="DML_INSERT",
            sql=("INSERT INTO dws_rental_daily SELECT DATE(dt) AS stat_date, org_id, "
                 "SUM(amount) AS daily_amt FROM db_dwd.fct_payment "
                 "GROUP BY DATE(dt), org_id"),
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_monthly", sql_type="DDL",
            sql=("CREATE TABLE dws_rental_monthly (stat_month DATE, org_id VARCHAR(255), "
                 "monthly_amt DECIMAL(20,2)) UNIQUE KEY(stat_month, org_id)"),
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_monthly", sql_type="DML_INSERT",
            sql=("INSERT INTO dws_rental_monthly SELECT "
                 "SUBSTRING(stat_date, 1, 7) AS stat_month, org_id, "
                 "SUM(daily_amt) AS monthly_amt FROM dws_rental_daily "
                 "GROUP BY SUBSTRING(stat_date, 1, 7), org_id"),
        ),
    ]

    rewritten = _rewrite_warehouse_plan_table_refs(state, plan)
    assert rewritten >= 1
    monthly_dml = next(
        a for a in plan.sql_artifacts
        if a.target_table == "dws_rental_monthly" and a.sql_type == "DML_INSERT"
    )
    assert "db_dws.dws_rental_daily" in monthly_dml.sql
    assert plan.dws_designs[1].source_tables == ["db_dws.dws_rental_daily"]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    checks = {f["check"] for f in failures}
    assert "warehouse_source_table_explored" not in checks
    assert "warehouse_sources_qualified" not in checks
    assert "dws_fields_materialized_in_load_sql" not in checks


def test_warehouse_sibling_dws_still_catches_hallucinated_join_column():
    """P1-4 must not paper over real errors: referencing a column absent from
    the sibling DWS's DDL is still reported, with available columns."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dwd": "db_dwd", "dim": "db_dim", "dws": "db_dws", "ads": "db_ads",
            },
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {"warehouse#dwd": SchemaExploration(
        source_db_id="warehouse#dwd",
        tables=[TableSchema(
            name="fct_payment", schema_name="db_dwd", db_type="doris",
            fields=[FieldSchema(name="amount", data_type="DECIMAL(20,2)")],
        )],
    )}

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [
        DWSDesign(
            summary_name="daily", summary_table="dws_rental_daily",
            source_dwd_table="db_dwd.fct_payment", source_tables=["db_dwd.fct_payment"],
            fields=[{"name": "stat_date", "type": "DATE"}],
            measures=[{"name": "daily_amt", "expression": "SUM(amount)"}],
            group_by_dimensions=["stat_date"], grain=["stat_date"],
            requirement_coverage=[{
                "requirement": "daily amount",
                "status": "supported",
                "evidence": ["stat_date", "daily_amt"],
                "gap_reason": "",
            }],
        ),
        DWSDesign(
            summary_name="monthly", summary_table="dws_rental_monthly",
            source_dwd_table="dws_rental_daily", source_tables=["dws_rental_daily"],
            fields=[{"name": "stat_month", "type": "DATE"}],
            measures=[{"name": "monthly_amt", "expression": "SUM(daily_amt)"}],
            group_by_dimensions=["stat_month"], grain=["stat_month"],
            requirement_coverage=[{
                "requirement": "monthly amount",
                "status": "supported",
                "evidence": ["stat_month", "monthly_amt"],
                "gap_reason": "",
            }],
        ),
    ]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_rental_daily", sql_type="DDL",
            sql="CREATE TABLE dws_rental_daily (stat_date DATE, daily_amt DECIMAL(20,2)) UNIQUE KEY(stat_date)",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_daily", sql_type="DML_INSERT",
            sql="INSERT INTO dws_rental_daily (stat_date, daily_amt) SELECT DATE(dt) AS stat_date, SUM(amount) AS daily_amt FROM db_dwd.fct_payment GROUP BY DATE(dt)",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_monthly", sql_type="DDL",
            sql="CREATE TABLE dws_rental_monthly (stat_month DATE, monthly_amt DECIMAL(20,2)) UNIQUE KEY(stat_month)",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_rental_monthly", sql_type="DML_INSERT",
            sql=("INSERT INTO dws_rental_monthly SELECT SUBSTRING(d.stat_date, 1, 7) AS stat_month, "
                 "SUM(d.daily_amt) AS monthly_amt, d.hallucinated_col "
                 "FROM dws_rental_daily d GROUP BY SUBSTRING(d.stat_date, 1, 7)"),
        ),
    ]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert any(f["check"] == "warehouse_source_columns_exist" for f in failures)
    assert any("d.hallucinated_col" in f["detail"] for f in failures)
    assert any("Available columns:" in f["detail"] for f in failures)


def test_repair_hint_guides_the_revise_llm():
    """P1-3: every invariant failure carries an actionable repair hint."""
    from wren_agent.nodes.validation import _repair_hint

    assert "EXPLICIT target column list" in _repair_hint(
        {"check": "dws_fields_materialized_in_load_sql", "detail": "x"})
    assert "Available columns" in _repair_hint(
        {"check": "warehouse_source_columns_exist", "detail": "x"})
    assert "landing database" in _repair_hint(
        {"check": "warehouse_sources_qualified", "detail": "x"})
    assert _repair_hint({"check": "unknown_check", "detail": "x"}) == ""


def test_repair_hint_period_column_and_invented_attr():
    """Task 9f42f542: point at stat_month; tell the LLM to drop org_name."""
    from wren_agent.nodes.validation import _repair_hint

    detail = (
        "ads_gz_test.ads_stock_inventory_monthly references columns absent "
        "from explored source `dws_gz_test.dws_stock_inventory_monthly`: "
        "['stat_date', 'org_name']. Available columns: sku_code, "
        "sku_name, monthly_after_number_sum, org_id, stat_month"
    )
    hint = _repair_hint({"check": "warehouse_source_columns_exist", "detail": detail})
    assert "stat_month" in hint
    assert "stat_date" in hint
    assert "org_name" in hint
    assert "do NOT invent" in hint


def test_repair_hint_dim_code_family():
    """Task 449dc3ca: point guessed card_code at explored dim_code."""
    from wren_agent.nodes.validation import _repair_hint

    detail = (
        "dws_gz_test.dws_stock_inventory_monthly references columns absent "
        "from explored source `dim_gz_test.dim_sku`: "
        "['b.card_code', 'b.card_name']. Available columns: sku_id, "
        "depreciation_method_name, dim_code, dim_name, effective_date, "
        "expiration_date, is_current, specifications, storage_location, "
        "use_life_month"
    )
    hint = _repair_hint({"check": "warehouse_source_columns_exist", "detail": detail})
    assert "dim_code" in hint
    assert "dim_name" in hint
    assert "card_code" in hint
    assert "card_name" in hint


def test_repair_hint_line_count_to_row_count():
    from wren_agent.nodes.validation import _repair_hint

    detail = (
        "ads_gz_test.ads_stock_inventory_monthly references columns absent "
        "from explored source `dws_gz_test.dws_stock_inventory_monthly`: "
        "['monthly_inventory_line_count']. Available columns: sku_code, "
        "monthly_inventory_row_count, org_id, stat_month"
    )
    hint = _repair_hint({"check": "warehouse_source_columns_exist", "detail": detail})
    assert "monthly_inventory_row_count" in hint
    assert "line_count" in hint


def test_repair_metadata_updates_parent_metrics():
    """Regression 93479659: the repair LLM must be able to fix design-level
    metadata (ADS derived-metric parent_metrics) that SQL repairs cannot
    reach."""
    from wren_agent.nodes.validation import _apply_repair_metadata

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="db_ads.ads_room_monthly",
        source_dws_tables=["db_dws.dws_room_monthly"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        metrics=[{
            "name": "平均合同金额", "field_name": "avg_contract_amount",
            "expression": "a / b", "metric_type": "derived",
            "parent_metrics": ["monthly_contract_count"],
        }],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["avg_contract_amount"], "gap_reason": "",
        }],
    )]

    ok = _apply_repair_metadata(plan, "ads_room_monthly", {"metrics": [{
        "name": "平均合同金额",
        "parent_metrics": ["monthly_contract_amount"],
    }]})
    assert ok is True
    assert plan.ads_designs[0].metrics[0]["parent_metrics"] == [
        "monthly_contract_amount",
    ]

    # Unknown target table → no-op, not an error
    assert _apply_repair_metadata(plan, "no_such_table", {"metrics": []}) is False


def test_repair_metadata_updates_requirement_coverage():
    """Regression f242a388: plan-level coverage gaps are repaired via
    requirement_coverage metadata — a table can claim a requirement the DWS
    deferred to ADS, replacing its partial entry with supported."""
    from wren_agent.nodes.validation import _apply_repair_metadata

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_room_monthly",
        source_dws_tables=["db_dws.dws_room_monthly"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        metrics=[],
        requirement_coverage=[{
            "requirement": "应收增长率（环比）派生指标",
            "status": "partial",
            "gap_reason": "需要多期数据，可在ADS层计算",
        }],
    )]

    ok = _apply_repair_metadata(plan, "ads_room_monthly", {
        "requirement_coverage": [{
            "requirement": "应收增长率（环比）派生指标",
            "status": "supported",
            "evidence": ["monthly_contract_amount", "LAG(monthly_contract_amount)"],
            "gap_reason": "",
        }],
    })
    assert ok is True
    entry = plan.ads_designs[0].requirement_coverage[0]
    assert entry["status"] == "supported"
    assert "monthly_contract_amount" in entry["evidence"]

    # New requirement claims are appended
    _apply_repair_metadata(plan, "ads_room_monthly", {
        "requirement_coverage": [{
            "requirement": "按商品分析",
            "status": "supported",
            "evidence": ["basics_room_id"],
            "gap_reason": "",
        }],
    })
    reqs = [c["requirement"] for c in plan.ads_designs[0].requirement_coverage]
    assert "按商品分析" in reqs


def test_repair_metadata_drops_declared_derived_fields():
    """Regression 513c12a5: derived ratio fields a DWS must not materialize
    are dropped from the declared fields via a metadata repair."""
    from wren_agent.nodes.validation import _apply_repair_metadata

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.dws_designs = [DWSDesign(
        summary_name="q", summary_table="db_dws.dws_quarterly",
        source_dwd_table="db_dwd.x", source_tables=["db_dwd.x"],
        fields=[
            {"name": "quarterly_amount_receivable", "type": "DECIMAL(20,2)"},
            {"name": "quarterly_collection_rate", "type": "DECIMAL(20,4)"},
            {"name": "quarterly_unpaid_fee_rate", "type": "DECIMAL(20,4)"},
        ],
        measures=[{"name": "amt", "expression": "SUM(x)"}],
        group_by_dimensions=["quarter_num"], grain=["quarter_num"],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["quarterly_amount_receivable"], "gap_reason": "",
        }],
    )]

    ok = _apply_repair_metadata(plan, "dws_quarterly", {
        "fields": ["quarterly_amount_receivable"],
    })
    assert ok is True
    names = [f["name"] for f in plan.dws_designs[0].fields]
    assert names == ["quarterly_amount_receivable"]


def test_repair_metadata_fields_append_and_qualified_match():
    """Fields repair appends names absent from the design and matches the
    target by bare name despite the db_ prefix."""
    from wren_agent.nodes.validation import _apply_repair_metadata

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.dws_designs = [DWSDesign(
        summary_name="d", summary_table="db_dws.dws_daily",
        source_dwd_table="db_dwd.x", source_tables=["db_dwd.x"],
        fields=[{"name": "stat_date", "type": "DATE"}],
        measures=[{"name": "amt", "expression": "SUM(x)"}],
        group_by_dimensions=["stat_date"], grain=["stat_date"],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["stat_date"], "gap_reason": "",
        }],
    )]
    ok = _apply_repair_metadata(plan, "dws_daily", {
        "fields": ["stat_date", "daily_amt"],
    })
    assert ok is True
    names = [f["name"] for f in plan.dws_designs[0].fields]
    assert names == ["stat_date", "daily_amt"]


def test_repair_metadata_fields_noop_without_fields_key():
    """Fields repair is a no-op when the metadata carries no fields key."""
    from wren_agent.nodes.validation import _apply_repair_metadata

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.dws_designs = [DWSDesign(
        summary_name="d", summary_table="dws_daily",
        source_dwd_table="db_dwd.x", source_tables=["db_dwd.x"],
        fields=[{"name": "stat_date", "type": "DATE"}],
        measures=[{"name": "amt", "expression": "SUM(x)"}],
        group_by_dimensions=["stat_date"], grain=["stat_date"],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["stat_date"], "gap_reason": "",
        }],
    )]
    _apply_repair_metadata(plan, "dws_daily", {"metrics": []})
    assert [f["name"] for f in plan.dws_designs[0].fields] == ["stat_date"]


def test_ads_cross_database_accepts_any_explored_dws_schema():
    """Regression 3dacf0ae: ADS load SQL anchored on an existing DWS table in
    an explored layer schema (e.g. dws_ecommerce_orders_sh) must pass — the
    old check hard-coded the landing db (db_dws) as the only valid prefix."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {"layers": {
            "dwd": "db_dwd", "dim": "db_dim", "dws": "db_dws", "ads": "db_ads",
        }},
    }], {}, source_mode="warehouse")
    state["explorations"] = {"wh#dws": SchemaExploration(source_db_id="wh#dws", tables=[
        TableSchema(
            name="dws_lease_collection_daily", schema_name="dws_ecommerce_orders_sh",
            db_type="doris",
            fields=[
                FieldSchema(name="stat_date", data_type="DATE"),
                FieldSchema(name="amount", data_type="DECIMAL(20,2)"),
            ],
        ),
    ])}

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_lease_collection_daily",
        source_dws_tables=["dws_ecommerce_orders_sh.dws_lease_collection_daily"],
        dimensions=["stat_date"],
        fields=[
            {"name": "stat_date", "type": "DATE"},
            {"name": "amount", "type": "DECIMAL(20,2)"},
        ],
        metrics=[{
            "name": "金额", "field_name": "amount", "expression": "amount",
            "metric_type": "atomic", "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["amount"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_lease_collection_daily", sql_type="DDL",
            sql="CREATE TABLE ads_lease_collection_daily (stat_date DATE, amount DECIMAL(20,2))",
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_lease_collection_daily", sql_type="DML_INSERT",
            sql="INSERT INTO ads_lease_collection_daily (stat_date, amount) "
                "SELECT stat_date, amount FROM dws_ecommerce_orders_sh.dws_lease_collection_daily",
        ),
    ]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert not any(f["check"] == "ads_cross_database_source" for f in failures)


def test_ads_cross_database_still_rejects_bare_refs():
    """Bare (unqualified) DWS refs in ADS load SQL still fail."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {"layers": {
            "dwd": "db_dwd", "dim": "db_dim", "dws": "db_dws", "ads": "db_ads",
        }},
    }], {}, source_mode="warehouse")

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_x",
        source_dws_tables=["dws_x"],
        dimensions=["stat_date"],
        fields=[{"name": "stat_date", "type": "DATE"}],
        metrics=[],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["stat_date"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DDL",
            sql="CREATE TABLE ads_x (stat_date DATE)",
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DML_INSERT",
            sql="INSERT INTO ads_x (stat_date) SELECT stat_date FROM dws_x",
        ),
    ]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert any(f["check"] == "ads_cross_database_source" for f in failures)


@pytest.mark.asyncio
async def test_doris_explain_catches_hallucinated_columns(monkeypatch):
    """Regression cc841bec/def559ca/42452474: Doris-native EXPLAIN catches
    hallucinated columns and dialect errors that regex cannot — no more
    per-column regex special-casing."""
    from wren_agent.nodes.validation import _doris_explain_load_sqls

    class FakeDoris:
        def __init__(self):
            self.calls = []

        async def query(self, sql, params=()):
            self.calls.append(sql)
            if "quarterly_unpaid_fee" in sql:
                raise RuntimeError(
                    "errCode = 2, detailMessage = Unknown column "
                    "'quarterly_unpaid_fee' in 'table list'"
                )
            return []

        def close(self):
            pass

    fake = FakeDoris()
    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient", lambda: fake
    )

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DML_INSERT",
            sql=("INSERT INTO ads_x (stat_month, unpaid) "
                 "SELECT stat_month, quarterly_unpaid_fee "
                 "FROM db_dws.dws_payment_collection_monthly"),
        ),
    ]

    failures = await _doris_explain_load_sqls(state, plan)
    assert len(failures) == 1
    assert failures[0]["check"] == "doris_dml_explain"
    assert "quarterly_unpaid_fee" in failures[0]["detail"]
    assert fake.calls[0].startswith("EXPLAIN ")


@pytest.mark.asyncio
async def test_doris_explain_skips_plan_internal_tables(monkeypatch):
    """DML referencing plan-internal tables (created only at deploy) is
    skipped — Doris reports those as 'Unknown column in table list' with no
    way to distinguish them from real column errors."""
    from wren_agent.nodes.validation import _doris_explain_load_sqls

    class FakeDoris:
        async def query(self, sql, params=()):
            raise AssertionError("must not be called")

        def close(self):
            pass

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient", lambda: FakeDoris()
    )
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="d", summary_table="dws_rent_monthly",
        source_dwd_table="db_dwd.x", source_tables=["db_dwd.x"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        measures=[{"name": "amt", "expression": "SUM(x)"}],
        group_by_dimensions=["stat_month"], grain=["stat_month"],
        requirement_coverage=[{
            "requirement": "r", "status": "supported",
            "evidence": ["stat_month"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DML_INSERT",
            sql=("INSERT INTO ads_x (stat_month, amt) "
                 "SELECT stat_month, amt FROM db_dws.dws_rent_monthly"),
        ),
    ]

    failures = await _doris_explain_load_sqls(state, plan)
    assert failures == []


@pytest.mark.asyncio
async def test_row_count_query_splits_landing_prefix(monkeypatch):
    """Regression 2cd30e16: a DWS summary_table named `db_dws.dws_x` must be
    counted via database=db_dws + bare table name — backticking the whole
    dotted name makes Doris fail with 'Table does not exist'."""
    from wren_agent.nodes.validation import _query_materialized_target_counts

    calls: list[tuple[str | None, str]] = []

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def get_row_count(self, table):
            calls.append((self.database, table))
            return 5

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient", FakeDoris
    )
    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {"layers": {"dws": "db_dws", "ads": "db_ads"}},
    }], {}, source_mode="warehouse")
    plan = _valid_materialized_plan()
    plan.dws_designs[0].summary_table = "db_dws.dws_collection_daily"

    counts = await _query_materialized_target_counts(state, plan)

    assert ("db_dws", "dws_collection_daily") in calls
    assert counts["db_dws.dws_collection_daily"] == 5


@pytest.mark.asyncio
async def test_row_count_follows_insert_into_v2_not_leftover_plan_name(monkeypatch):
    """Task 2c04cf95: leftover collision rewrote INSERT INTO t_v2, but
    plan.ads_table stayed t. Counting t (empty leftover) failed final
    validation while t_v2 held the load."""
    from wren_agent.nodes.validation import _query_materialized_target_counts

    calls: list[tuple[str | None, str]] = []

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def get_row_count(self, table):
            calls.append((self.database, table))
            return 0 if table == "ads_stock_inventory_quality" else 8

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient", FakeDoris
    )
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = []
    plan.ads_designs = [ADSDesign(
        application_name="quality",
        ads_table="ads_gz_test.ads_stock_inventory_quality",
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS",
            target_table="ads_gz_test.ads_stock_inventory_quality",
            sql_type="DML_INSERT",
            sql=(
                "INSERT INTO `ads_gz_test`.`ads_stock_inventory_quality_v2` "
                "(`org_id`) SELECT a.org_id FROM `dws_gz_test`.`dws_x` a"
            ),
        ),
    ]

    counts = await _query_materialized_target_counts(state, plan)

    assert ("ads_gz_test", "ads_stock_inventory_quality_v2") in calls
    assert ("ads_gz_test", "ads_stock_inventory_quality") not in calls
    assert counts["ads_gz_test.ads_stock_inventory_quality_v2"] == 8
    assert all(n > 0 for n in counts.values())


@pytest.mark.asyncio
async def test_row_count_query_uses_split_business_task_targets(monkeypatch):
    from wren_agent.nodes.validation import _query_materialized_target_counts

    calls = []
    class FakeDoris:
        def __init__(self, database=None): self.database = database
        async def get_row_count(self, table):
            calls.append((self.database, table)); return 3
    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", FakeDoris)

    state = create_initial_state("t", "goal", [], {}, source_mode="business_db",
                                 business_task_type="ods_migration",
                                 warehouse_source_id="wh",
                                 landing={"ods": "db_ods", "dim": "db_dim", "dwd": "db_dwd"})
    plan = _valid_materialized_plan()
    plan.ods_designs = [type("ODS", (), {"ods_table": "ods_orders"})()]
    plan.dws_designs = []
    plan.ads_designs = []
    counts = await _query_materialized_target_counts(state, plan)
    assert ("db_ods", "ods_orders") in calls
    assert counts["ods_orders"] == 3


@pytest.mark.asyncio
async def test_row_count_query_uses_core_model_fact_table(monkeypatch):
    """Regression 6895f7dd: core_model counts DWD via fact_table, not dwd_table."""
    from wren_agent.nodes.validation import _query_materialized_target_counts

    calls = []
    class FakeDoris:
        def __init__(self, database=None): self.database = database
        async def get_row_count(self, table):
            calls.append((self.database, table)); return 4
    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", FakeDoris)

    state = create_initial_state("t", "goal", [], {}, source_mode="business_db",
                                 business_task_type="core_model",
                                 warehouse_source_id="wh",
                                 landing={"ods": "db_ods", "dim": "db_dim", "dwd": "db_dwd"})
    plan = _valid_materialized_plan()
    plan.dim_designs = [DIMDesign(
        dim_name="store", dim_table="dim_house", source="from_source_table",
    )]
    plan.dwd_designs = [DWDDesign(
        fact_name="contract", fact_table="dwd_order_house_contract_snapshot",
    )]
    plan.dws_designs = []
    plan.ads_designs = []
    counts = await _query_materialized_target_counts(state, plan)
    assert ("db_dim", "dim_house") in calls
    assert ("db_dwd", "dwd_order_house_contract_snapshot") in calls
    assert counts["dim_house"] == 4
    assert counts["dwd_order_house_contract_snapshot"] == 4


@pytest.mark.asyncio
async def test_final_validate_uses_observed_doris_counts(monkeypatch):
    from wren_agent.nodes.validation import final_validate

    async def counts(*_args, **_kwargs):
        return {"dws_collection_daily": 12, "ads_collection": 12}

    monkeypatch.setattr(
        "wren_agent.nodes.validation._query_materialized_target_counts", counts
    )
    state = create_initial_state("t", "goal", [], {})
    state["design_plan"] = _valid_materialized_plan()
    state["execution_results"] = [ExecutionResult(task_instance_id="1", success=True)]

    result = await final_validate(state)

    assert result["all_validations_passed"] is True
    assert result["execution_results"][-1].rows_written["ads_collection"] == 12


@pytest.mark.asyncio
async def test_report_preserves_failure_and_counts_answered_questions(monkeypatch):
    from wren_agent.nodes.reporting import generate_report

    class FakeLLM:
        async def chat(self, *_args, **_kwargs):
            return "- Failed truthfully."

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client", lambda: FakeLLM()
    )
    state = create_initial_state("t", "goal", [], {})
    state["status"] = TaskStatus.FAILED
    state["design_plan"] = _valid_materialized_plan()
    # Post-execution failure (data quality) keeps the full report; only
    # pre-deployment failures get the plain-language failure report.
    state["quality_checks"] = {"failed": 1, "errored": 0, "total": 1}
    clarification = ClarificationItem(question="口径?", answer="已确认")
    clarification.status = ClarificationStatus.ANSWERED
    state["clarifications"] = [clarification]

    result = await generate_report(state)

    assert result["status"] == TaskStatus.FAILED
    assert "已回答: 1" in result["construction_report"]
    assert "**建模目标**：goal" in result["plain_report"]
    assert "尚未达到可直接使用的标准" in result["plain_report"]


def test_plain_report_shows_goal_and_explored_data_for_persisted_state():
    from wren_agent.nodes.reporting import build_plain_report

    exploration = SchemaExploration(
        source_db_id="order-db",
        tables=[TableSchema(
            name="order_payment",
            schema_name="business",
            db_type="mysql",
            comment="订单金额收款记录",
            fields=[
                FieldSchema(name="community_id", data_type="bigint", comment="小区编号"),
                FieldSchema(name="paid_amount", data_type="decimal", comment="实收金额"),
            ],
        )],
    )
    plan = _valid_materialized_plan().model_dump(mode="json")
    plan["dws_designs"][0]["source_tables"] = ["order_payment"]
    state = {
        "task_id": "plain-report-task",
        "modeling_goal": "按月查看各小区订单金额支付情况",
        "status": "completed",
        "design_plan": plan,
        "explorations": {"order-db#business": exploration.model_dump(mode="json")},
        "validation_results": [{"passed": True}],
        "execution_results": [{"rows_written": {"ads_collection": 120}}],
        "quality_checks": {"passed": 3, "failed": 0, "errored": 0},
        "errors": [],
        "warnings": [],
    }

    report = build_plain_report(state)

    assert "## 1. 这次想解决什么问题" in report
    assert "按月查看各小区订单金额支付情况" in report
    assert "order_payment" in report
    assert "订单金额收款记录" in report
    assert "小区编号" in report
    assert "120 行数据写入目标表" in report
    assert "## 5. 业务人员下一步建议" in report


# ── Case-sensitive Doris database refs (task a1af4bed) ──────────


def _case_mismatch_fixture():
    """The a1af4bed shape: DML joins a dim table with a lowercased db prefix
    while the explored schema (and the sibling dws prefix) uses `_SH`."""
    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dim": ["dim_ecommerce_SH"],
                "dws": ["dws_ecommerce_orders_sh"],
                "ads": ["db_ads"],
            },
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dim": SchemaExploration(
            source_db_id="wh#dim",
            tables=[TableSchema(
                name="dim_order_shop", schema_name="dim_ecommerce_SH",
                db_type="doris",
                fields=[FieldSchema(name="member_bk", data_type="VARCHAR(64)"),
                        FieldSchema(name="house_name", data_type="VARCHAR(255)")],
            )],
        ),
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[TableSchema(
                name="dws_order_payment_analysis_quarter",
                schema_name="dws_ecommerce_orders_sh", db_type="doris",
                fields=[FieldSchema(name="building_id", data_type="VARCHAR(64)"),
                        FieldSchema(name="quarter_id", data_type="VARCHAR(32)"),
                        FieldSchema(name="quarterly_amount_receivable",
                                    data_type="DECIMAL(20,2)")],
            )],
        ),
    }

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="shop quarter receivable",
        summary_table="dws_order_rental_receivable_building_quarter",
        source_dwd_table="dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter",
        source_tables=[
            "dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter",
            "dim_ecommerce_sh.dim_order_shop",
        ],
        fields=[{"name": "building_id", "type": "VARCHAR(64)"},
                {"name": "amount_receivable", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "amount_receivable", "expression": "SUM(quarterly_amount_receivable)"}],
        group_by_dimensions=["building_id", "quarter_id"],
        grain=["building_id", "quarter_id"],
        requirement_coverage=[{
            "requirement": "receivable by shop",
            "status": "supported",
            "evidence": ["building_id", "amount_receivable"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_order_rental_receivable_building_quarter",
            sql_type="DDL",
            sql=("CREATE TABLE dws_order_rental_receivable_building_quarter "
                 "(building_id VARCHAR(64), quarter_id VARCHAR(32), "
                 "amount_receivable DECIMAL(20,2)) UNIQUE KEY(building_id, quarter_id)"),
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_order_rental_receivable_building_quarter",
            sql_type="DML_INSERT",
            sql=("INSERT INTO dws_order_rental_receivable_building_quarter "
                 "(building_id, quarter_id, amount_receivable) "
                 "SELECT src.building_id AS building_id, src.quarter_id AS quarter_id, "
                 "SUM(src.quarterly_amount_receivable) AS amount_receivable "
                 "FROM dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter src "
                 "LEFT JOIN dim_ecommerce_sh.dim_order_shop b "
                 "ON b.member_bk = src.building_id "
                 "GROUP BY src.building_id, src.quarter_id"),
        ),
    ]
    return state, plan


def test_unique_explored_name_to_schema_preserves_case():
    from wren_agent.nodes.validation import _unique_explored_name_to_schema

    state, _plan = _case_mismatch_fixture()
    mapping = _unique_explored_name_to_schema(state)
    assert mapping["dim_order_shop"] == "dim_ecommerce_SH"
    assert mapping["dws_order_payment_analysis_quarter"] == \
        "dws_ecommerce_orders_sh"


def test_rewrite_fixes_lowercase_dim_db_prefix():
    """Regression a1af4bed: a `dim_ecommerce_sh.` prefix must be
    rewritten to the explored casing `dim_ecommerce_SH`, both in the
    DML SQL and in design.source_tables."""
    from wren_agent.nodes.validation import _rewrite_warehouse_plan_table_refs

    state, plan = _case_mismatch_fixture()
    rewritten = _rewrite_warehouse_plan_table_refs(state, plan)
    assert rewritten >= 2

    dml = next(a for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
    assert "dim_ecommerce_SH.dim_order_shop" in dml.sql
    assert "dim_ecommerce_sh." not in dml.sql
    assert "dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter" in dml.sql
    assert plan.dws_designs[0].source_tables == [
        "dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter",
        "dim_ecommerce_SH.dim_order_shop",
    ]


def test_db_ref_failure_flags_case_mismatch():
    """The safety net: when the rewrite cannot fire, validation must reject a
    case-mismatched db prefix instead of shipping it to Doris."""
    from wren_agent.nodes.validation import (
        _validate_warehouse_load_sql,
        _warehouse_known_dbs,
    )

    state, plan = _case_mismatch_fixture()
    known = _warehouse_known_dbs(state)
    assert "dim_ecommerce_SH" in known

    dml = next(a for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
    # un-rewritten SQL — validate the ORIGINAL lowercase form
    checks = _validate_warehouse_load_sql(
        plan.dws_designs[0].summary_table,
        [dml],
        state,
        plan,
    )
    assert any(
        f.get("check") == "warehouse_source_db_case_mismatch"
        and "dim_ecommerce_sh" in f.get("detail", "")
        and "dim_ecommerce_SH" in f.get("detail", "")
        for f in checks
    ), f"expected case-mismatch failure, got: {checks}"


def test_db_ref_failure_unknown_db():
    from wren_agent.nodes.validation import (
        _validate_warehouse_load_sql,
    )

    state, plan = _case_mismatch_fixture()
    dml = next(a for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
    unknown = dml.sql.replace(
        "dim_ecommerce_sh.dim_order_shop",
        "invented_db.dim_order_shop",
    )
    dml.sql = unknown
    checks = _validate_warehouse_load_sql(
        plan.dws_designs[0].summary_table,
        [dml],
        state,
        plan,
    )
    assert any(
        f.get("check") == "warehouse_source_db_unknown"
        and "invented_db" in f.get("detail", "")
        for f in checks
    ), f"expected unknown-db failure, got: {checks}"


def test_db_ref_failure_passes_for_exact_case():
    """Correct-casing refs must not trip the new checks (no regression for
    sound designs that qualified sources with the real db name)."""
    from wren_agent.nodes.validation import (
        _validate_warehouse_load_sql,
    )

    state, plan = _case_mismatch_fixture()
    dml = next(a for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
    fixed = dml.sql.replace(
        "dim_ecommerce_sh.dim_order_shop",
        "dim_ecommerce_SH.dim_order_shop",
    )
    dml.sql = fixed
    checks = _validate_warehouse_load_sql(
        plan.dws_designs[0].summary_table,
        [dml],
        state,
        plan,
    )
    assert not any(
        f.get("check", "").startswith("warehouse_source_db")
        for f in checks
    ), f"expected no db failures, got: {checks}"



# ── Doris table name length limit + full task-log fetch (task a1af4bed) ──


def _long_name_plan(state) -> DWDesignPlan:
    long_name = "dws_sales_order_rental_receivable_analysis_unit_building_room_quarter"  # 66 chars
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="unit shop product quarter",
        summary_table=long_name,
        source_dwd_table="dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter",
        source_tables=["dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter"],
        fields=[{"name": "building_id", "type": "VARCHAR(64)"},
                {"name": "amount_receivable", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "amount_receivable", "expression": "SUM(quarterly_amount_receivable)"}],
        group_by_dimensions=["building_id", "quarter_id"],
        grain=["building_id", "quarter_id"],
        requirement_coverage=[{
            "requirement": "receivable by shop",
            "status": "supported",
            "evidence": ["building_id", "amount_receivable"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table=long_name, sql_type="DDL",
            sql=(f"CREATE TABLE {long_name} (building_id VARCHAR(64), "
                 "quarter_id VARCHAR(32), amount_receivable DECIMAL(20,2)) "
                 "UNIQUE KEY(building_id, quarter_id)"),
        ),
        SQLArtifact(
            layer="DWS", target_table=long_name, sql_type="DML_INSERT",
            sql=("INSERT INTO " + long_name + " (building_id, quarter_id, amount_receivable) "
                 "SELECT building_id, quarter_id, SUM(quarterly_amount_receivable) "
                 "FROM dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter "
                 "GROUP BY building_id, quarter_id"),
        ),
    ]
    return plan


def test_table_name_length_limit_fails_66_char_name(monkeypatch):
    """Regression a1af4bed: Doris rejects table names over its configured
    table_name_length_limit — validation must catch it before deploy instead
    of at DS execution. (Limit is configurable; tests pin it to 64.)"""
    from wren_agent.nodes.validation import _validate_materialization_plan

    import wren_agent.nodes.validation as validation_mod
    monkeypatch.setattr(validation_mod, "_DORIS_TABLE_NAME_LIMIT", 64)

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")
    plan = _long_name_plan(state)
    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert any(
        f.get("check") == "warehouse_table_name_too_long"
        and str(len(plan.dws_designs[0].summary_table)) in f.get("detail", "")
        for f in failures
    ), f"expected length failure, got: {failures}"


def test_table_name_length_passes_under_limit():
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")
    plan = _long_name_plan(state)
    plan.dws_designs[0].summary_table = "dws_rental_building_quarter"
    plan.sql_artifacts[0].target_table = "dws_rental_building_quarter"
    plan.sql_artifacts[0].sql = (
        "CREATE TABLE dws_rental_building_quarter (building_id VARCHAR(64), "
        "quarter_id VARCHAR(32), amount_receivable DECIMAL(20,2)) "
        "UNIQUE KEY(building_id, quarter_id)")
    plan.sql_artifacts[1].target_table = "dws_rental_building_quarter"
    plan.sql_artifacts[1].sql = (
        "INSERT INTO dws_rental_building_quarter (building_id, quarter_id, amount_receivable) "
        "SELECT building_id, quarter_id, SUM(quarterly_amount_receivable) "
        "FROM dws_ecommerce_orders_sh.dws_order_payment_analysis_quarter "
        "GROUP BY building_id, quarter_id")
    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert not any(
        f.get("check") == "warehouse_table_name_too_long"
        for f in failures
    ), f"expected no length failure, got: {failures}"


@pytest.mark.asyncio
async def test_fetch_full_task_log_paginates():
    """Regression a1af4bed: the DS log API truncates at `limit` lines — the
    full-log fetcher must paginate with skip_line_num until the log ends."""
    from wren_agent.nodes.deployment import _fetch_full_task_log

    class FakeDS:
        def __init__(self):
            self.calls = []

        async def query_task_log(self, task_instance_id, *, skip_line_num=0, limit=200):
            self.calls.append((skip_line_num, limit))
            if skip_line_num == 0:
                return "line0\nline1\nline2\nline3"      # exactly `limit`... fewer
            return ""                                     # exhausted

    ds = FakeDS()
    text = await _fetch_full_task_log(ds, 42, chunk=4)
    assert "line0" in text and "line3" in text
    assert ds.calls == [(0, 4), (4, 4)]


@pytest.mark.asyncio
async def test_fetch_full_task_log_keeps_paging_past_first_chunk():
    from wren_agent.nodes.deployment import _fetch_full_task_log

    class FakeDS:
        def __init__(self):
            self.calls = []

        async def query_task_log(self, task_instance_id, *, skip_line_num=0, limit=200):
            self.calls.append(skip_line_num)
            if skip_line_num == 0:
                return "\n".join(f"c0l{i}" for i in range(4))   # full chunk
            if skip_line_num == 4:
                return "error line beyond the window"            # short chunk → stop
            return ""

    ds = FakeDS()
    text = await _fetch_full_task_log(ds, 42, chunk=4)
    assert "c0l0" in text and "error line beyond the window" in text
    assert ds.calls == [0, 4]



# ── Deterministic table-name shortening (tasks a1af4bed / b338672b) ──


def test_shorten_plan_table_names_propagates_rename(monkeypatch):
    """A 65-char DWS name must be mechanically shortened and every reference
    (DDL, DML, source_tables, sibling DML) rewritten consistently."""
    from wren_agent.nodes.validation import _rewrite_warehouse_plan_table_refs

    import wren_agent.nodes.validation as validation_mod
    monkeypatch.setattr(validation_mod, "_DORIS_TABLE_NAME_LIMIT", 64)

    long_name = "dws_sales_order_receivable_analysis_unit_building_room_quarter_extra"  # 65
    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="deep grain",
        summary_table=long_name,
        source_dwd_table="dws_ecommerce_orders_sh.dws_order_rental_receivable_unit_building_room_quarter",
        source_tables=["dws_ecommerce_orders_sh.dws_order_rental_receivable_unit_building_room_quarter"],
        fields=[{"name": "building_id", "type": "VARCHAR(64)"}],
        measures=[{"name": "amount_receivable", "expression": "SUM(amount_receivable)"}],
        group_by_dimensions=["building_id", "quarter_id"],
        grain=["building_id", "quarter_id"],
        requirement_coverage=[{
            "requirement": "deep grain", "status": "supported",
            "evidence": ["building_id"], "gap_reason": "",
        }],
    )]
    plan.ads_designs = [ADSDesign(
        application_name="dashboard",
        ads_table="ads_deep_grain",
        source_dws_tables=[long_name],
        dimensions=["building_id"],
        fields=[{"name": "building_id", "type": "VARCHAR(64)"}],
        metrics=[{
            "name": "amount", "field_name": "amount_receivable",
            "expression": "amount_receivable", "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "deep grain", "status": "supported",
            "evidence": ["building_id"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table=long_name, sql_type="DDL",
            sql=(f"CREATE TABLE IF NOT EXISTS dws_ecommerce_orders_sh.{long_name} "
                 "(building_id VARCHAR(64), amount_receivable DECIMAL(20,2)) "
                 "UNIQUE KEY(building_id)"),
        ),
        SQLArtifact(
            layer="DWS", target_table=long_name, sql_type="DML_INSERT",
            sql=(f"INSERT INTO {long_name} (building_id, amount_receivable) "
                 "SELECT building_id, SUM(amount_receivable) "
                 f"FROM dws_ecommerce_orders_sh.{long_name} "
                 "GROUP BY building_id"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_deep_grain", sql_type="DML_INSERT",
            sql=f"INSERT INTO ads_deep_grain SELECT building_id, amount_receivable FROM {long_name}",
        ),
    ]

    rewritten = _rewrite_warehouse_plan_table_refs(state, plan)

    assert rewritten >= 3
    new_name = plan.dws_designs[0].summary_table
    assert len(new_name.split(".")[-1].strip("`")) <= 64
    assert new_name != long_name
    # name keeps its semantic core — only filler segments were stripped
    assert new_name.endswith("unit_building_room_quarter_extra")
    # propagated everywhere
    assert plan.ads_designs[0].source_dws_tables == [new_name]
    assert plan.sql_artifacts[0].target_table == new_name
    for a in plan.sql_artifacts:
        assert long_name not in a.sql, f"old name left in artifact SQL: {a.sql}"
        assert new_name in a.sql



# ── design_revise creates missing artifacts (task 00317106) ──


@pytest.mark.asyncio
async def test_llm_repair_creates_missing_artifact(monkeypatch):
    """Regression 00317106: a declared table with NO artifacts
    (`materialization_sql_complete: found []`) must get its missing DDL/DML
    CREATED from the repair, not silently dropped."""
    from wren_agent.nodes.validation import design_revise

    plan = _valid_materialized_plan()
    # contract_month is declared but has zero artifacts
    ghost = DWSDesign(
        summary_name="contract month",
        summary_table="dws_contract_month",
        source_dwd_table="dws_collection_daily",
        source_tables=["dws_collection_daily"],
        fields=[{"name": "stat_month", "type": "DATE"},
                {"name": "contract_amt", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "contract_amt", "expression": "SUM(contract_amt)"}],
        group_by_dimensions=["stat_month"],
        grain=["stat_month"],
        requirement_coverage=[{
            "requirement": "contract monthly",
            "status": "supported",
            "evidence": ["stat_month", "contract_amt"],
            "gap_reason": "",
        }],
    )
    plan.dws_designs = list(plan.dws_designs) + [ghost]

    from wren_agent.models.task import ValidationLevel, ValidationResult

    state = create_initial_state("t", "goal", [], {})
    state["design_plan"] = plan
    state["design_revision_count"] = 1
    state["validation_results"] = [ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="DWS",
        passed=False,
        checks=[],
        failures=[{
            "check": "materialization_sql_complete",
            "detail": ("dws_contract_month must have both DDL and DML load "
                       "SQL; found []"),
        }],
        semantic_assertions=[],
    )]
    state["warnings"] = []

    # Fake repair LLM: emits the missing DDL + DML for the ghost table
    async def fake_generate_json(*args, **kwargs):
        return (
            '{"repairs": ['
            '{"target_table": "dws_contract_month", "sql_type": "DDL", '
            '"sql": "CREATE TABLE dws_contract_month (stat_month DATE, contract_amt DECIMAL(20,2)) UNIQUE KEY(stat_month)"},'
            '{"target_table": "dws_contract_month", "sql_type": "DML_INSERT", '
            '"sql": "INSERT INTO dws_contract_month (stat_month, contract_amt) SELECT stat_month, SUM(contract_amt) FROM dws_collection_daily GROUP BY stat_month"}'
            "]}"
        )

    # design_revise now routes repairs through generate_typed_json (schema
    # validation + regeneration); delegate to the fake's raw JSON and validate
    # it the same way the real client would. `schema` arrives positionally
    # (and `self` is bound first because the fake lives in the class dict).
    async def fake_generate_typed_json(self, system_prompt, user_message, schema, *args, **kw):
        return schema.model_validate_json(
            await fake_generate_json(system_prompt, user_message, *args, **kw)
        )

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client",
        lambda: type("FakeLLM", (), {
            "generate_json": fake_generate_json,
            "generate_typed_json": fake_generate_typed_json,
        })(),
    )

    new_state = await design_revise(state)

    created = [
        a for a in plan.sql_artifacts
        if a.target_table == "dws_contract_month"
    ]
    assert len(created) == 2, f"expected 2 created artifacts, got {created}"
    assert {a.sql_type for a in created} == {"DDL", "DML_INSERT"}
    assert any("CREATE TABLE dws_contract_month" in a.sql for a in created)



# ── Invented target db prefix rewrite (task c0f31ae0) ──


def test_fix_plan_target_db_prefixes(monkeypatch):
    """An ADS target with an invented `ads_ecommerce_SH.` prefix must be
    rewritten to the landing db (`db_ads`) in the design AND every artifact."""
    from wren_agent.nodes.validation import _rewrite_warehouse_plan_table_refs

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dws": ["dws_ecommerce_orders_sh"],
                "ads": ["db_ads", "ads_ecommerce_BJ", "wren_sandbox"],
            },
            "landing": {"dws": "dws_ecommerce_orders_sh", "ads": "db_ads"},
        },
    }], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="dashboard",
        ads_table="ads_ecommerce_SH.ads_order_revenue_contract_month",
        source_dws_tables=["dws_ecommerce_orders_sh.dws_order_revenue_unit_month"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        metrics=[{
            "name": "amount", "field_name": "amount_receivable",
            "expression": "amount_receivable", "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "contract monthly",
            "status": "supported",
            "evidence": ["stat_month", "amount_receivable"],
            "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_ecommerce_SH.ads_order_revenue_contract_month",
            sql_type="DDL",
            sql=("CREATE TABLE IF NOT EXISTS ads_ecommerce_SH.ads_order_revenue_contract_month "
                 "(stat_month DATE, amount_receivable DECIMAL(20,2))"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_ecommerce_SH.ads_order_revenue_contract_month",
            sql_type="DML_INSERT",
            sql=("INSERT INTO ads_ecommerce_SH.ads_order_revenue_contract_month "
                 "(stat_month, amount_receivable) "
                 "SELECT stat_month, SUM(amount_receivable) "
                 "FROM dws_ecommerce_orders_sh.dws_order_revenue_unit_month "
                 "GROUP BY stat_month"),
        ),
    ]

    rewritten = _rewrite_warehouse_plan_table_refs(state, plan)

    assert rewritten >= 2
    assert plan.ads_designs[0].ads_table == \
        "db_ads.ads_order_revenue_contract_month"
    for a in plan.sql_artifacts:
        assert a.target_table == "db_ads.ads_order_revenue_contract_month"
        assert "ads_ecommerce_SH." not in a.sql
        assert "db_ads.ads_order_revenue_contract_month" in a.sql



# ── ADS DDL landing-db enforcement (task a2b0cea0) ──


def _landing_state():
    return create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {
                "dws": ["dws_ecommerce_orders_sh"],
                "ads": ["db_ads", "ads_ecommerce_BJ", "wren_sandbox"],
            },
            "landing": {"dws": "dws_ecommerce_orders_sh", "ads": "db_ads"},
        },
    }], {}, source_mode="warehouse")


def test_ddl_target_db_check_catches_cross_layer_prefix():
    """An ADS DDL that creates its table in the DWS database must fail
    `warehouse_ddl_target_db` even when the design target is bare."""
    from wren_agent.nodes.validation import _validate_materialization_plan

    state = _landing_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="dashboard",
        ads_table="ads_lease_receivable_room_month",   # bare target
        source_dws_tables=["dws_ecommerce_orders_sh.dws_x"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        metrics=[{
            "name": "amount", "field_name": "amount_receivable",
            "expression": "amount_receivable", "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "monthly", "status": "supported",
            "evidence": ["stat_month"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_lease_receivable_room_month",
            sql_type="DDL",
            sql=("CREATE TABLE IF NOT EXISTS dws_ecommerce_orders_sh.ads_lease_receivable_room_month "
                 "(stat_month DATE, amount_receivable DECIMAL(20,2))"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_lease_receivable_room_month",
            sql_type="DML_INSERT",
            sql=("INSERT INTO ads_lease_receivable_room_month (stat_month, amount_receivable) "
                 "SELECT stat_month, SUM(amount_receivable) "
                 "FROM dws_ecommerce_orders_sh.dws_x GROUP BY stat_month"),
        ),
    ]

    results = _validate_materialization_plan(plan, state)
    failures = [f for result in results for f in result.failures]
    assert any(
        f.get("check") == "warehouse_ddl_target_db"
        and "dws_ecommerce_orders_sh" in f.get("detail", "")
        for f in failures
    ), f"expected ddl-target-db failure, got: {failures}"


def test_rewrite_fixes_sql_level_cross_layer_prefix(monkeypatch):
    """A bare ADS target whose DDL SQL qualifies with the DWS landing db is
    deterministically rewritten to the ADS landing db."""
    from wren_agent.nodes.validation import _rewrite_warehouse_plan_table_refs

    state = _landing_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="dashboard",
        ads_table="ads_lease_receivable_room_month",
        source_dws_tables=["dws_ecommerce_orders_sh.dws_x"],
        dimensions=["stat_month"],
        fields=[{"name": "stat_month", "type": "DATE"}],
        metrics=[{
            "name": "amount", "field_name": "amount_receivable",
            "expression": "amount_receivable", "metric_type": "atomic",
            "parent_metrics": [],
        }],
        requirement_coverage=[{
            "requirement": "monthly", "status": "supported",
            "evidence": ["stat_month"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="ADS", target_table="ads_lease_receivable_room_month",
            sql_type="DDL",
            sql=("CREATE TABLE IF NOT EXISTS dws_ecommerce_orders_sh.ads_lease_receivable_room_month "
                 "(stat_month DATE, amount_receivable DECIMAL(20,2))"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_lease_receivable_room_month",
            sql_type="DML_INSERT",
            sql=("INSERT INTO dws_ecommerce_orders_sh.ads_lease_receivable_room_month "
                 "(stat_month, amount_receivable) "
                 "SELECT stat_month, SUM(amount_receivable) "
                 "FROM dws_ecommerce_orders_sh.dws_x GROUP BY stat_month"),
        ),
    ]

    _rewrite_warehouse_plan_table_refs(state, plan)

    ddl = next(a for a in plan.sql_artifacts if a.sql_type == "DDL")
    assert "db_ads.ads_lease_receivable_room_month" in ddl.sql
    assert "dws_ecommerce_orders_sh.ads_lease_receivable_room_month" not in ddl.sql
    dml = next(a for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
    # the DWS source prefix must survive — only the TARGET prefix changes
    assert "FROM dws_ecommerce_orders_sh.dws_x" in dml.sql


def test_landing_db_injected_into_ads_prompt():
    """The ADS design prompt must state the ADS landing database so the LLM
    stops inventing/borrowing prefixes."""
    from wren_agent.nodes.design_layers import _build_layer_context

    state = _landing_state()
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")
    ctx = _build_layer_context(state, "ADS")
    assert "ADS layer tables land in database `db_ads`" in ctx["system_prompt"]
    assert "DWS layer tables land in database `dws_ecommerce_orders_sh`" in ctx["system_prompt"]
    # DDL clause order rule present
    assert "COMMENT must NEVER precede the KEY clause" in ctx["system_prompt"]



# ── 0-row sources: visible counts + validation gate (tasks 582633e8/73e47237) ──


def test_exploration_serialization_shows_zero_rows_as_empty():
    from wren_agent.models.schema import FieldSchema as _FS

    """`row_count_estimate=0` is falsy — it must still render as `0 [EMPTY]`
    or the LLM cannot tell empty tables from unknown-count ones."""
    from wren_agent.nodes.design_layers import _serialize_explorations_for_prompt

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[
                TableSchema(
                    name="dws_empty_table", schema_name="dws_ecommerce_orders_sh",
                    db_type="doris", row_count_estimate=0,
                    fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
                ),
                TableSchema(
                    name="dws_full_table", schema_name="dws_ecommerce_orders_sh",
                    db_type="doris", row_count_estimate=1234,
                    fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
                ),
            ],
        ),
    }
    # 0-row tables are now EXCLUDED from the design context entirely (the
    # [EMPTY] mark did not stop the LLM anchoring on them — task 432c17b1);
    # the data-bearing table must still render with its count.
    text = _serialize_explorations_for_prompt(state)
    assert "dws_empty_table" not in text
    assert "Row count estimate: 1,234" in text


def test_validate_rejects_zero_row_source():
    """A DML anchored on a 0-row explored table must fail
    `warehouse_source_table_empty` before deploy."""
    from wren_agent.models.schema import FieldSchema as _FS
    from wren_agent.nodes.validation import _validate_warehouse_load_sql

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[TableSchema(
                name="dws_empty_table", schema_name="dws_ecommerce_orders_sh",
                db_type="doris", row_count_estimate=0,
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
    }
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    artifact = SQLArtifact(
        layer="DWS", target_table="dws_new", sql_type="DML_INSERT",
        sql=("INSERT INTO dws_new (amount) SELECT SUM(amount) "
             "FROM dws_ecommerce_orders_sh.dws_empty_table"),
    )
    checks = _validate_warehouse_load_sql("dws_new", [artifact], state, plan)
    assert any(
        f.get("check") == "warehouse_source_table_empty"
        and "0 rows" in f.get("detail", "")
        for f in checks
    ), f"expected empty-source failure, got: {checks}"



# ── Layered sourcing: DWS must not read other DWS tables (task 432c17b1) ──


def test_dws_source_layer_check_rejects_dws_sources():
    from wren_agent.models.schema import FieldSchema as _FS
    """A DWS DML reading an explored dws-layer table must fail
    `warehouse_dws_source_layer`; ADS reading DWS stays legal."""
    from wren_agent.nodes.validation import _validate_warehouse_load_sql

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dwd": ["dwd_ecommerce_orders_sh"], "dws": ["dws_ecommerce_orders_sh"]},
        },
    }], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dwd": SchemaExploration(
            source_db_id="wh#dwd",
            tables=[TableSchema(
                name="dwd_detail", schema_name="dwd_ecommerce_orders_sh",
                db_type="doris", row_count_estimate=100,
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[TableSchema(
                name="dws_summary", schema_name="dws_ecommerce_orders_sh",
                db_type="doris", row_count_estimate=50,
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
    }
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="x", summary_table="dws_new",
        source_dwd_table="dwd_ecommerce_orders_sh.dwd_detail",
        source_tables=["dws_ecommerce_orders_sh.dws_summary"],
        fields=[{"name": "amount", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "amount", "expression": "SUM(amount)"}],
        group_by_dimensions=["g"], grain=["g"],
        requirement_coverage=[{
            "requirement": "x", "status": "supported",
            "evidence": ["amount"], "gap_reason": "",
        }],
    )]
    art = SQLArtifact(
        layer="DWS", target_table="dws_new", sql_type="DML_INSERT",
        sql=("INSERT INTO dws_new (amount) SELECT SUM(amount) "
             "FROM dws_ecommerce_orders_sh.dws_summary"),
    )
    checks = _validate_warehouse_load_sql("dws_new", [art], state, plan)
    assert any(
        f.get("check") == "warehouse_dws_source_layer" for f in checks
    ), f"expected dws-source-layer failure, got: {checks}"

    # ADS reading the same dws table is legitimate
    art2 = SQLArtifact(
        layer="ADS", target_table="ads_new", sql_type="DML_INSERT",
        sql=("INSERT INTO ads_new (amount) SELECT SUM(amount) "
             "FROM dws_ecommerce_orders_sh.dws_summary"),
    )
    checks2 = _validate_warehouse_load_sql("ads_new", [art2], state, plan, target_layer="ADS")
    assert not any(
        f.get("check") == "warehouse_dws_source_layer" for f in checks2
    ), f"ADS must be allowed to read DWS, got: {checks2}"



# ── DDL probe: real-landing execution before DS (task 36437f66) ──


@pytest.mark.asyncio
async def test_ddl_probe_surfaces_real_doris_errors(monkeypatch):
    """A DDL that Doris rejects must surface as a `ddl_probe` failure with
    the REAL error text, grouped by the artifact layer."""
    from wren_agent.nodes.validation import _probe_execute_layer_ddls

    state = create_initial_state("t", "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_bad", sql_type="DDL",
            sql=("CREATE TABLE dws_bad (a VARCHAR(10)) "
                 "COMMENT 'x' DUPLICATE KEY(a)"),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_ok", sql_type="DDL",
            sql="CREATE TABLE ads_ok (a VARCHAR(10))",
        ),
    ]

    class FakeClient:
        def __init__(self, **kw):
            self.calls = []

        async def execute(self, sql):
            self.calls.append(sql)
            if "dws_bad" in sql:
                raise RuntimeError(
                    "errCode = 2, detailMessage = mismatched input 'DUPLICATE'")

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda **kw: FakeClient(**kw),
    )

    failures = await _probe_execute_layer_ddls(state, plan)

    assert len(failures) == 1, f"expected 1 failure, got {failures}"
    assert failures[0]["check"] == "ddl_probe"
    assert failures[0]["layer"] == "DWS"
    assert "mismatched input 'DUPLICATE'" in failures[0]["detail"]



# ── Deterministic DDL backfill (safe subset, task a59491c4) ──


def test_backfill_declared_columns_with_value_source():
    """A declared field missing from the DDL but computed in the load SQL
    (SELECT ... AS x) gets backfilled into the DDL and the INSERT target
    list at the aligned position."""
    from wren_agent.nodes.validation import _backfill_declared_columns

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="x", summary_table="dws_x",
        source_dwd_table="dwd_x", source_tables=["dwd_x"],
        fields=[
            {"name": "stat_date", "type": "DATE", "comment": "日期"},
            {"name": "already_amount", "type": "DECIMAL(20,2)", "comment": "已收金额"},
        ],
        measures=[],
        group_by_dimensions=["stat_date"], grain=["stat_date"],
        requirement_coverage=[{
            "requirement": "x", "status": "supported",
            "evidence": ["stat_date"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_x", sql_type="DDL",
            sql=("CREATE TABLE dws_x (stat_date DATE) "
                 "UNIQUE KEY(stat_date) COMMENT 'x'"),
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_x", sql_type="DML_INSERT",
            sql=("INSERT INTO dws_x (stat_date) "
                 "SELECT stat_date, SUM(amount) AS already_amount FROM dwd_x "
                 "GROUP BY stat_date"),
        ),
    ]

    n = _backfill_declared_columns({}, plan)

    assert n >= 2  # DDL column + INSERT target
    ddl = plan.sql_artifacts[0].sql
    assert "already_amount DECIMAL(20,2) COMMENT '已收金额'" in ddl
    dml = plan.sql_artifacts[1].sql
    assert "INSERT INTO dws_x (stat_date, already_amount)" in dml


def test_backfill_skips_fields_without_value_source():
    """A declared field the load SQL does NOT compute must NOT be
    mechanically added — that would be a silent wrong-data risk."""
    from wren_agent.nodes.validation import _backfill_declared_columns

    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="x", summary_table="dws_x",
        source_dwd_table="dwd_x", source_tables=["dwd_x"],
        fields=[
            {"name": "stat_date", "type": "DATE", "comment": "日期"},
            {"name": "ghost_field", "type": "DECIMAL(20,2)", "comment": "臆造"},
        ],
        measures=[],
        group_by_dimensions=["stat_date"], grain=["stat_date"],
        requirement_coverage=[{
            "requirement": "x", "status": "supported",
            "evidence": ["stat_date"], "gap_reason": "",
        }],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_x", sql_type="DDL",
            sql="CREATE TABLE dws_x (stat_date DATE) UNIQUE KEY(stat_date) COMMENT 'x'",
        ),
        SQLArtifact(
            layer="DWS", target_table="dws_x", sql_type="DML_INSERT",
            sql=("INSERT INTO dws_x (stat_date) "
                 "SELECT stat_date FROM dwd_x GROUP BY stat_date"),
        ),
    ]

    n = _backfill_declared_columns({}, plan)

    assert n == 0
    assert "ghost_field" not in plan.sql_artifacts[0].sql
