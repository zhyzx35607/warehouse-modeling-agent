"""Warehouse-mode (数仓探索) tests — wayfinder map for dual-mode exploration.

Covers the decisions locked in docs/wayfinder/map.md (T-01..T-11):
source_mode plumbing, warehouse exploration branch, design short-circuits,
existing-table upstream serialization, insufficient_warehouse clarifications,
and the DAG/DEPENDENT helpers.
"""

import pytest
from wren_agent.models.design import DWDesignPlan, DWSDesign
from wren_agent.models.schema import SchemaExploration, TableSchema
from wren_agent.models.task import ClarificationItem, SourceDatabase, TaskStatus
from wren_agent.state import create_initial_state


def _warehouse_source(**overrides) -> SourceDatabase:
    base = dict(
        id="wh-cluster-1",
        name="wh_cluster",
        db_type="doris",
        host="127.0.0.1",
        port=9030,
        database="wren_dw",
        mcp_server_name="mcp_wh_cluster",
        connection_params={
            "layers": {
                "ods": "db_ods", "dim": "db_dwd", "dwd": "db_dwd",
                "dwm": "", "dws": "db_dws", "ads": "db_ads",
            },
        },
    )
    base.update(overrides)
    return SourceDatabase(**base)


# ── State plumbing (T-07 / T-10) ────────────────────────────────


def test_initial_state_defaults_to_business_db():
    state = create_initial_state("t1", "goal", [], {})
    assert state["source_mode"] == "business_db"
    assert state["insufficient_gaps"] == []


def test_initial_state_accepts_warehouse_mode():
    state = create_initial_state("t1", "goal", [], {}, source_mode="warehouse")
    assert state["source_mode"] == "warehouse"


def test_source_database_carries_layers():
    sdb = _warehouse_source()
    assert sdb.layers == {}
    # layers live in connection_params (T-01/T-10) — extracted by the API layer


def test_build_source_databases_extracts_layers():
    from wren_agent.api.routes import _build_source_databases
    payload = [{
        "id": "wh-cluster-1", "name": "wh", "db_type": "doris",
        "host": "h", "port": 9030, "database": "wren_dw",
        "connection_params": {"layers": {"dim": "db_dwd", "dwd": "db_dwd",
                                         "dws": "db_dws", "ads": "db_ads"}},
    }]
    dbs = _build_source_databases(payload)
    assert dbs[0].layers == {"dim": "db_dwd", "dwd": "db_dwd",
                             "dws": "db_dws", "ads": "db_ads"}
    assert dbs[0].id == "wh-cluster-1"


# ── Registry validation (T-01) ──────────────────────────────────


def test_validate_doris_layers_requires_six_layers():
    from wren_agent.api.source_dbs import _validate_doris_layers
    # One database hosting multiple layers dedupes; wren_dw-style single db
    layers = _validate_doris_layers({"layers": {
        "ods": "o", "dim": "d", "dwd": "d", "dwm": "d", "dws": "w", "ads": "a"}})
    assert layers == ["o", "d", "w", "a"]


def test_validate_doris_layers_allows_empty_layers():
    # Auto-discovery/stale-removal may legitimately empty a layer (e.g. no dwm)
    from wren_agent.api.source_dbs import _validate_doris_layers
    assert _validate_doris_layers({"layers": {"dim": "d"}}) == ["d"]
    assert _validate_doris_layers(
        {"layers": {"ods": ["o1", "o2"], "dim": "", "dwm": "d"}}) == ["o1", "o2", "d"]
    with pytest.raises(ValueError):
        _validate_doris_layers({})
    with pytest.raises(ValueError):
        _validate_doris_layers({"layers": {}})


# ── Exploration branch (T-07 / T-06) ────────────────────────────


@pytest.mark.asyncio
async def test_warehouse_exploration_iterates_layers(monkeypatch):
    from wren_agent.nodes import explore

    class FakeConnector:
        def __init__(self, **kw):
            self.database = kw["database"]
            self.db_type = kw["db_type"]

        async def list_tables(self):
            return {
                "db_dwd": ["dwd_sale_order", "dim_customer"],
                "db_dws": ["dws_sale_order_daily"],
            }.get(self.database, [])

        async def get_tables_info(self, table_names):
            return [
                TableSchema(
                    name=table_name, schema_name=self.database, db_type="doris",
                    fields=[], row_count_estimate=None,
                )
                for table_name in table_names
            ]

        async def get_foreign_keys(self):
            return []

        async def aclose(self):
            return None

    import wren_agent.tools.db_explorer as db_explorer_mod
    monkeypatch.setattr(db_explorer_mod, "DBConnector", FakeConnector)

    async def _extract(sdb):
        return {
            "db_id": sdb.id, "name": sdb.name, "db_type": sdb.db_type,
            "host": sdb.host, "port": sdb.port, "user": "u", "password": "p",
            "database": sdb.database, "credential_source": "inline",
            "connection_params": sdb.connection_params,
        }

    monkeypatch.setattr(explore, "_extract_db_params", _extract)

    state = create_initial_state(
        "t-wh", "销售订单日汇总", [_warehouse_source()], {}, source_mode="warehouse"
    )
    state = await explore.schema_explore_light(state)

    keys = list(state["explorations"].keys())
    # ods/ads skipped; dwd iterated once despite hosting both dim+dwd layers
    assert "wh-cluster-1#dwd" in keys
    assert "wh-cluster-1#dws" in keys
    assert "wh-cluster-1#ods" not in keys
    assert "wh-cluster-1#ads" not in keys
    assert all("#" in k for k in keys)


@pytest.mark.asyncio
async def test_warehouse_exploration_filters_databases(monkeypatch):
    """库级选择: connection_params.explore_databases restricts which mapped
    databases are explored (empty list = none; absent key = all)."""
    from wren_agent.nodes import explore

    class FakeConnector:
        def __init__(self, **kw):
            self.database = kw["database"]
            self.db_type = kw["db_type"]

        async def list_tables(self):
            return {
                "db_dwd": ["dwd_sale_order", "dim_customer"],
                "db_dws": ["dws_sale_order_daily"],
            }.get(self.database, [])

        async def get_tables_info(self, table_names):
            return [
                TableSchema(
                    name=table_name, schema_name=self.database, db_type="doris",
                    fields=[], row_count_estimate=None,
                )
                for table_name in table_names
            ]

        async def get_foreign_keys(self):
            return []

        async def aclose(self):
            return None

    import wren_agent.tools.db_explorer as db_explorer_mod
    monkeypatch.setattr(db_explorer_mod, "DBConnector", FakeConnector)

    async def _extract(sdb):
        return {
            "db_id": sdb.id, "name": sdb.name, "db_type": sdb.db_type,
            "host": sdb.host, "port": sdb.port, "user": "u", "password": "p",
            "database": sdb.database, "credential_source": "inline",
            "connection_params": sdb.connection_params,
        }

    monkeypatch.setattr(explore, "_extract_db_params", _extract)

    # Only db_dws is checked → dwd layer has nothing to explore
    src = _warehouse_source()
    src.connection_params = {**src.connection_params, "explore_databases": ["db_dws"]}
    state = create_initial_state(
        "t-wh2", "销售订单日汇总", [src], {}, source_mode="warehouse"
    )
    state = await explore.schema_explore_light(state)
    assert list(state["explorations"].keys()) == ["wh-cluster-1#dws"]

    # Empty list = explore nothing
    src2 = _warehouse_source()
    src2.connection_params = {**src2.connection_params, "explore_databases": []}
    state2 = create_initial_state(
        "t-wh3", "销售订单日汇总", [src2], {}, source_mode="warehouse"
    )
    state2 = await explore.schema_explore_light(state2)
    assert state2["explorations"] == {}


def test_warehouse_layers_landing_override():
    """库级落库选择: landing override wins; multi-db layers normalize to one."""
    from wren_agent.models.task import SourceDatabase
    from wren_agent.nodes.deployment import _warehouse_layers

    src = SourceDatabase(
        name="wh", db_type="doris", host="h", port=9030, database="wren_dw",
        mcp_server_name="mcp_wh",
        connection_params={
            "layers": {
                "ods": "db_ods", "dim": ["db_dwd", "db_dim2"], "dwd": "db_dwd",
                "dwm": "", "dws": ["db_dws", "db_dws2"], "ads": "db_ads",
            },
            "landing": {"dws": "db_dws2"},
        },
    )
    state = create_initial_state("t", "g", [src], {}, source_mode="warehouse")
    out = _warehouse_layers(state)
    assert out["dws"] == "db_dws2"   # landing override wins
    assert out["ads"] == "db_ads"    # no override -> single value
    assert out["dim"] == "db_dwd"    # multi-db layer -> first
    assert out["dwm"] == ""          # empty layer stays empty


def test_warehouse_datasource_name_is_per_layer_and_db():
    """Datasource names carry the landing database — the shared
    `doris_dws`/`doris_ads` names were reused across tasks with different
    landings, so DWS tables landed in the first task's database while load
    SQL referenced the current landing db (task 67b6907f)."""
    from wren_agent.nodes.deployment import _warehouse_datasource_name

    assert _warehouse_datasource_name("DWS", "db_dws") == "doris_dws_db_dws"
    assert (
        _warehouse_datasource_name("DWS", "dws_ecommerce_orders_sh")
        == "doris_dws_dws_ecommerce_orders_sh"
    )
    assert _warehouse_datasource_name("ADS", "db_ads") == "doris_ads_db_ads"
    # distinct landings never share a datasource
    assert (
        _warehouse_datasource_name("DWS", "db_dws")
        != _warehouse_datasource_name("DWS", "dws_ecommerce_orders_sh")
    )


@pytest.mark.asyncio
async def test_warehouse_mode_skips_sensitive_confirmation():
    from wren_agent.nodes import explore
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["sensitive_fields"] = []
    result = await explore.confirm_sensitive(state)
    assert result["clarifications"] == []
    assert result.get("pending_human_input") is False


@pytest.mark.asyncio
async def test_sensitive_confirmation_is_idempotent_on_recovery():
    from wren_agent.models.task import SensorResult, SensorType
    from wren_agent.nodes import explore

    state = create_initial_state("t", "goal", [], {})
    state["sensitive_fields"] = [
        SensorResult(
            table_name="customer",
            field_name="name",
            sensor_type=SensorType.NAME,
            confidence=0.9,
        )
    ]

    await explore.confirm_sensitive(state)
    await explore.confirm_sensitive(state)

    assert len(state["clarifications"]) == 1
    assert state["clarifications"][0].blocking is False


@pytest.mark.asyncio
async def test_warehouse_mode_skips_drift_detection():
    from wren_agent.nodes.deployment import detect_schema_drifts
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    result = await detect_schema_drifts(state)
    assert result["schema_drifts"] == []
    assert "current_snapshots" not in result or result["current_snapshots"] == {}


# ── Design chain (T-08) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ods_dim_dwd_short_circuit_in_warehouse_mode():
    from wren_agent.nodes.design_layers import design_ods, design_dim, design_dwd
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")

    for node in (design_ods, design_dim, design_dwd):
        result = await node(dict(state))
        assert result["design_plan"].ods_designs == []
        assert result["design_plan"].dim_designs == []
        assert result["design_plan"].dwd_designs == []


def test_dws_warehouse_explored_tables_come_from_exploration_section():
    """DWS design anchors on explored tables via the `## Source Schema
    Explorations` section (in _build_layer_context) — the upstream section no
    longer re-dumps them (task c0f31ae0: duplicated tables doubled every
    design prompt past the LLM timeout)."""
    from wren_agent.nodes.design_layers import (
        _build_layer_context,
        _serialize_upstream_designs,
    )
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    exp = SchemaExploration(source_db_id="wh#dwd", phase="light")
    exp.tables.append(TableSchema(
        name="dwd_sale_order", schema_name="db_dwd", db_type="doris",
        fields=[], row_count_estimate=100,
    ))
    state["explorations"] = {"wh#dwd": exp}

    # upstream: no duplicate dump
    text = _serialize_upstream_designs(state, "DWS")
    assert "db_dwd.dwd_sale_order" not in text

    # the design prompt still carries the table via the exploration section
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")
    ctx = _build_layer_context(state, "DWS")
    assert "## Source Schema Explorations" in ctx["user_message"]
    assert "dwd_sale_order" in ctx["user_message"]


def _plan_with_dws() -> DWDesignPlan:
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs.append(DWSDesign(
        summary_name="订单应收月度汇总",
        summary_table="dws_order_revenue_unit_month",
        source_dwd_table="dwd_order_payment_cycle_acc",
        fields=[
            {"name": "unit_id", "comment": "单位ID"},
        ],
        measures=[
            {"name": "daily_amount_receivable", "comment": "月度应收金额"},
            {"name": "daily_amount_paid", "comment": "月度实收金额"},
        ],
        group_by_dimensions=["unit_id", "snapshot_month"],
        grain=["unit_id", "snapshot_month"],
    ))
    return plan


def test_warehouse_ads_prompt_includes_this_plan_dws_designs():
    """Warehouse-mode ADS design must see the plan's own DWS designs —
    they are not yet in the warehouse, so the existing-tables serialization
    alone hides them and ADS falls back to DWD sources, breaking
    derived_metric_parent_chain (task 0de4bbfb)."""
    from wren_agent.nodes.design_layers import _serialize_upstream_designs
    # a real modeling goal so the detail filter keeps the receivable table
    state = create_initial_state(
        "t", "对销售订单的应收实收进行建模分析（店铺、商品、月度季度）",
        [], {}, source_mode="warehouse")
    exp = SchemaExploration(source_db_id="wh#dwd", phase="light")
    exp.tables.append(TableSchema(
        name="dwd_sale_order", schema_name="db_dwd", db_type="doris",
        fields=[], row_count_estimate=100,
    ))
    state["explorations"] = {"wh#dwd": exp}
    state["design_plan"] = _plan_with_dws()

    text = _serialize_upstream_designs(state, "ADS")
    assert "This Plan's DWS Layer" in text
    assert "dws_order_revenue_unit_month" in text
    assert "daily_amount_receivable" in text
    assert "daily_amount_paid" in text
    assert "source_dws_tables" in text  # explicit anchor instruction


def test_plan_dws_detail_filtered_by_modeling_goal():
    """ADS injection must filter DWS detail rows by modeling-goal relevance —
    full-detail injection bloated the prompt past the LLM timeout (task
    a1af4bed). All table NAMES stay visible; only goal-relevant tables get
    field/measure detail."""
    from wren_agent.nodes.design_layers import _serialize_plan_dws_designs

    plan = _plan_with_dws()  # dws_order_revenue_unit_month (payment/receivable)
    plan.dws_designs.append(DWSDesign(
        summary_name="车位月结", summary_table="dws_parking_month_end",
        source_dwd_table="dwd_parking_fee",
        grain=["stat_month"], group_by_dimensions=["stat_month"],
        measures=[{"name": "daily_fee", "comment": "月度车位费"}],
        fields=[{"name": "stat_month", "comment": "月份"},
                {"name": "daily_fee", "comment": "月度车位费"}],
    ))
    goal = "对销售订单的应收实收进行建模分析，各个月、季度的店铺、商品收款的深度分析"

    text = _serialize_plan_dws_designs(plan, goal=goal)
    # both table names listed
    assert "dws_order_revenue_unit_month" in text
    assert "dws_parking_month_end" in text
    # goal-relevant table gets measure detail; irrelevant one is name-only
    assert "daily_amount_receivable" in text
    assert "daily_fee" not in text
    assert "not goal-relevant" in text


def test_plan_dws_detail_all_when_goal_empty():
    """Without a goal (business-DB upstream path), every table gets detail."""
    from wren_agent.nodes.design_layers import _serialize_plan_dws_designs
    plan = _plan_with_dws()
    text = _serialize_plan_dws_designs(plan, goal="")
    assert "daily_amount_receivable" in text


def test_warehouse_dws_prompt_does_not_inject_plan_dws():
    """DWS design itself must NOT see its own (not-yet-existing) designs —
    only existing warehouse tables (no circular self-anchoring)."""
    from wren_agent.nodes.design_layers import _serialize_upstream_designs
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    exp = SchemaExploration(source_db_id="wh#dwd", phase="light")
    exp.tables.append(TableSchema(
        name="dwd_sale_order", schema_name="db_dwd", db_type="doris",
        fields=[], row_count_estimate=100,
    ))
    state["explorations"] = {"wh#dwd": exp}
    state["design_plan"] = _plan_with_dws()

    text = _serialize_upstream_designs(state, "DWS")
    assert "This Plan's DWS Layer" not in text
    assert "dws_order_revenue_unit_month" not in text


def test_business_db_ads_prompt_includes_plan_dws_designs():
    """Business-DB mode already serializes the plan DWS layer for ADS via the
    layer-order path — guard against regression (same parent_metrics failure
    class as task 0de4bbfb)."""
    from wren_agent.nodes.design_layers import _serialize_upstream_designs
    state = create_initial_state("t", "goal", [], {}, source_mode="business_db")
    state["design_plan"] = _plan_with_dws()

    text = _serialize_upstream_designs(state, "ADS")
    assert "Upstream: DWS Layer" in text
    assert "dws_order_revenue_unit_month" in text
    assert "daily_amount_receivable" in text


def test_business_db_each_layer_sees_all_its_upstreams():
    """Every business-DB layer's upstream serialization must include ALL
    preceding layers (ODS→DIM→DWD→DWS→ADS chain) — no invisible upstream."""
    from wren_agent.nodes.design_layers import _serialize_upstream_designs
    from wren_agent.models.design import DWDDesign, DIMDesign, ODSDesign

    plan = _plan_with_dws()
    plan.ods_designs.append(ODSDesign(
        ods_table="ods_sale_order", source_table="t_sale_order",
        source_db_id="src-1", sync_method="full",
        schedule_frequency="daily", fields=[],
    ))
    plan.dim_designs.append(DIMDesign(
        dim_table="dim_room", dim_name="商品维", source="from_source_table",
        source_table="t_room", surrogate_key="room_id",
        natural_key=["source_room_id"], fields=[],
    ))
    plan.dwd_designs.append(DWDDesign(
        fact_name="应收周期事实", fact_table="dwd_receivable_cycle",
        business_process="t_receivable", source_ods_tables=["ods_sale_order"],
        fields=[], measures=[], dimension_refs=["dim_room"], grain=["id"],
    ))
    state = create_initial_state("t", "goal", [], {}, source_mode="business_db")
    state["design_plan"] = plan

    # DIM sees ODS; DWD sees ODS+DIM; DWS sees ODS+DIM+DWD; ADS sees all four.
    assert "ods_sale_order" in _serialize_upstream_designs(state, "DIM")
    dwd_text = _serialize_upstream_designs(state, "DWD")
    assert "ods_sale_order" in dwd_text and "dim_room" in dwd_text
    dws_text = _serialize_upstream_designs(state, "DWS")
    assert "ods_sale_order" in dws_text and "dim_room" in dws_text and "dwd_receivable_cycle" in dws_text
    ads_text = _serialize_upstream_designs(state, "ADS")
    for name in ("ods_sale_order", "dim_room", "dwd_receivable_cycle",
                 "dws_order_revenue_unit_month"):
        assert name in ads_text


def test_dws_upstream_lists_all_table_names_beyond_cap():
    """The plan-DWS serialization must list EVERY table name even when there
    are more than the per-table detail cap — a truncated name list hides
    tables from ADS and breaks parent_metrics/lineage (task 0de4bbfb)."""
    from wren_agent.nodes.design_layers import _serialize_plan_dws_designs

    plan = _plan_with_dws()
    for i in range(30):
        plan.dws_designs.append(DWSDesign(
            summary_name=f"汇总{i}",
            summary_table=f"dws_extra_summary_{i}",
            source_dwd_table="dwd_x",
            measures=[{"name": f"daily_m{i}", "comment": f"指标{i}"}],
            group_by_dimensions=["id"], grain=["id"],
        ))
    text = _serialize_plan_dws_designs(plan)
    # Table #29 is beyond the 25-detail cap but MUST appear in the all-names
    # "Tables:" line; its measure detail is capped.
    assert "dws_extra_summary_29" in text
    assert "daily_m29" not in text
    assert "more DWS tables listed above by name only" in text
    assert text.index("dws_extra_summary_0") < text.index("dws_extra_summary_29")


def test_ods_migration_explore_ignores_table_cap():
    from wren_agent.nodes.explore import _select_tables_to_explore

    tables = [f"t{i}" for i in range(291)]
    selected, truncated = _select_tables_to_explore(
        tables, cap=200, business_task_type="ods_migration",
    )
    assert selected == tables
    assert truncated is False


def test_full_task_explore_respects_table_cap():
    from wren_agent.nodes.explore import _select_tables_to_explore

    tables = [f"t{i}" for i in range(291)]
    selected, truncated = _select_tables_to_explore(
        tables, cap=200, business_task_type="full",
    )
    assert selected == tables[:200]
    assert truncated is True


def test_relevant_table_filter_empty_on_vocabulary_miss():
    from wren_agent.nodes.explore import _filter_relevant_tables

    tables = ["dwd_receivable_cycle", "dim_parking_space", "dws_fee_monthly"]

    assert _filter_relevant_tables(tables, "分析罕见业务口径") == []


def test_relevant_table_filter_supports_parking_fee_arrears():
    from wren_agent.nodes.explore import _filter_relevant_tables

    tables = ["dim_parking_space", "dwd_receivable_cycle", "dwd_lease_contract"]
    result = _filter_relevant_tables(tables, "分析各小区车位管理费欠费情况")

    assert "dim_parking_space" in result
    assert "dwd_receivable_cycle" in result


# ── insufficient_warehouse clarifications (T-05) ────────────────


@pytest.mark.asyncio
async def test_consume_insufficient_abandon_cancels_task():
    from wren_agent.nodes.design_layers import _consume_insufficient_answers
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["clarifications"] = [ClarificationItem(
        question="数仓不足以支撑…", context="gaps",
        candidates=["调整需求", "部分支撑", "放弃"],
        clarification_type="insufficient_warehouse", answer="放弃",
    )]
    result = await _consume_insufficient_answers(state)
    assert result["status"] == TaskStatus.CANCELLED
    assert result["clarifications"] == []


@pytest.mark.asyncio
async def test_consume_insufficient_partial_records_gaps():
    from wren_agent.nodes.design_layers import _consume_insufficient_answers
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["clarifications"] = [ClarificationItem(
        question="缺区域维度", context="现有 dwd 无 region_id",
        candidates=["调整需求", "部分支撑", "放弃"],
        clarification_type="insufficient_warehouse", answer="部分支撑",
    )]
    result = await _consume_insufficient_answers(state)
    assert len(result["insufficient_gaps"]) == 1
    assert result["insufficient_gaps"][0]["type"] == "insufficient_warehouse"
    assert result["status"] != TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_consume_insufficient_revise_goal():
    from wren_agent.nodes.design_layers import _consume_insufficient_answers
    state = create_initial_state("t", "old goal", [], {}, source_mode="warehouse")
    state["clarifications"] = [ClarificationItem(
        question="q", context="c", candidates=["调整需求", "部分支撑", "放弃"],
        clarification_type="insufficient_warehouse", answer="调整需求：按客户维度出日趋势",
    )]
    result = await _consume_insufficient_answers(state)
    assert result["modeling_goal"] == "按客户维度出日趋势"


@pytest.mark.asyncio
async def test_raise_insufficient_creates_blocking_item():
    from wren_agent.nodes.design_layers import _raise_insufficient_warehouse
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {"wh#dwd": SchemaExploration(source_db_id="wh#dwd", phase="light")}
    await _raise_insufficient_warehouse(state, "DWS")
    assert len(state["clarifications"]) == 1
    item = state["clarifications"][0]
    assert item.clarification_type == "insufficient_warehouse"
    assert item.blocking is True
    assert item.candidates == ["调整需求", "部分支撑", "放弃"]
    assert state["pending_human_input"] is True


def test_route_after_design_interrupts_on_pending():
    from wren_agent.graph import route_after_design
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["clarifications"] = [ClarificationItem(
        question="q", context="c", candidates=["a", "b", "c"], blocking=True,
        clarification_type="insufficient_warehouse",
    )]
    assert route_after_design(state) == "wait_human"


def test_route_after_design_ends_on_cancelled():
    from wren_agent.graph import route_after_design
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["status"] = TaskStatus.CANCELLED
    assert route_after_design(state) == "__end__"


def test_layer_prefix_filter_single_db_mixed():
    from wren_agent.nodes.explore import _filter_layer_tables
    tables = ["dim_financial_counterparty", "dwd_payment_flow_detail",
              "dws_transaction_flow_daily", "ods_financial_x",
              "ads_demo", "fct_something", "misc_utility"]
    # dim layer: keeps dim_ + prefix-less, drops every other layer's prefixes
    dim = _filter_layer_tables(tables, "dim")
    assert "dim_financial_counterparty" in dim
    assert "misc_utility" in dim
    assert "dws_transaction_flow_daily" not in dim
    assert "ods_financial_x" not in dim
    assert "ads_demo" not in dim
    # dwd layer: keeps dwd_/dwm_/fct_, drops dim_/dws_/ods_/ads_
    dwd = _filter_layer_tables(tables, "dwd")
    assert "dwd_payment_flow_detail" in dwd
    assert "fct_something" in dwd
    assert "dim_financial_counterparty" not in dwd
    assert "dws_transaction_flow_daily" not in dwd
    # dws layer: only dws_ (+ prefix-less)
    dws = _filter_layer_tables(tables, "dws")
    assert dws == ["dws_transaction_flow_daily", "misc_utility"]


def test_dedicated_layer_db_keeps_fct_tables():
    """ecommerce / BJ: DB mapped to one layer must explore fct_ too."""
    from wren_agent.nodes.explore import _tables_for_warehouse_layer

    tables = [
        "dim_order_store",
        "fct_order_billing_event",
        "dws_should_not_matter_here",
    ]
    layers = {
        "dim": ["dim_ecommerce_bj"],
        "dwd": ["dwd_ecommerce_orders_bj"],
        "dws": ["dws_ecommerce_orders_bj"],
    }
    kept = _tables_for_warehouse_layer(
        tables, "dim", "dim_ecommerce_bj", layers,
    )
    assert "fct_order_billing_event" in kept
    assert "dim_order_store" in kept


def test_shared_db_still_prefix_filters_per_layer():
    from wren_agent.nodes.explore import _tables_for_warehouse_layer

    tables = [
        "dim_financial_counterparty",
        "fct_something",
        "dws_transaction_flow_daily",
    ]
    layers = {"dim": "wren_dw", "dwd": "wren_dw", "dws": "wren_dw"}
    dim = _tables_for_warehouse_layer(tables, "dim", "wren_dw", layers)
    dwd = _tables_for_warehouse_layer(tables, "dwd", "wren_dw", layers)
    assert "fct_something" not in dim
    assert "fct_something" in dwd
    assert "dim_financial_counterparty" in dim
    assert "dim_financial_counterparty" not in dwd


def test_route_after_design_reports_on_failed():
    from wren_agent.graph import route_after_design
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["status"] = TaskStatus.FAILED
    assert route_after_design(state) == "generate_report"


@pytest.mark.asyncio
async def test_route_after_validation_fails_fast_on_empty_design():
    """Incident regression (task 85b063e9): an empty design must not loop
    through design_revise → human_approval → dag_deploy → execute → error."""
    from wren_agent.graph import route_after_validation
    from wren_agent.nodes.validation import harness_validate
    state = create_initial_state("t", "goal", [], {})
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")  # no sql_artifacts
    # The node owns fail-fast marking; the route funnels FAILED → report.
    result = await harness_validate(state)
    assert route_after_validation(result) == "generate_report"
    assert result["status"] == TaskStatus.FAILED
    assert any("no SQL artifacts" in e for e in result["errors"])


def test_route_after_validation_still_revises_nonempty_failure():
    from wren_agent.graph import route_after_validation
    state = create_initial_state("t", "goal", [], {})
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [{"layer": "DWS", "sql_type": "DDL", "target_table": "t1", "sql": "x"}]
    state["design_plan"] = plan
    state["all_validations_passed"] = False
    assert route_after_validation(state) == "design_revise"


@pytest.mark.asyncio
async def test_raise_insufficient_auto_mode_fails_fast():
    from wren_agent.nodes.design_layers import _raise_insufficient_warehouse
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse", mode="auto")
    state["explorations"] = {"wh#dwd": SchemaExploration(source_db_id="wh#dwd", phase="light")}
    await _raise_insufficient_warehouse(state, "DWS")
    assert state["status"] == TaskStatus.FAILED
    assert state["clarifications"] == []
    assert state.get("pending_human_input") is not True


@pytest.mark.asyncio
async def test_raise_insufficient_manual_mode_keeps_htil():
    from wren_agent.nodes.design_layers import _raise_insufficient_warehouse
    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse", mode="manual")
    state["explorations"] = {"wh#dwd": SchemaExploration(source_db_id="wh#dwd", phase="light")}
    await _raise_insufficient_warehouse(state, "DWS")
    assert len(state["clarifications"]) == 1
    assert state["pending_human_input"] is True


def test_order_keywords_match_warehouse_tables():
    from wren_agent.nodes.explore import _filter_layer_tables, _filter_relevant_tables
    goal = "订单按月汇总"
    tables = _filter_layer_tables([
        "fct_order_payment_contract_acc",
        "dim_order_store",
        "dwd_payment_flow_detail",
        "__insert_overwrite_gate",
    ], "dwd")
    relevant = _filter_relevant_tables(tables, goal)
    assert "fct_order_payment_contract_acc" in relevant
    assert "dwd_payment_flow_detail" not in relevant


def test_route_after_design_continues_normally():
    from wren_agent.graph import route_after_design
    state = create_initial_state("t", "goal", [], {})
    assert route_after_design(state) == "harness_validate"


# ── DAG helpers (T-11) ──────────────────────────────────────────


def test_quartz_cron_hourly_monthly():
    from wren_agent.nodes.deployment import _quartz_cron
    assert _quartz_cron("hourly") == "0 0 * * * ? *"
    assert _quartz_cron("monthly") == "0 0 0 1 * ? *"
    assert _quartz_cron("weekly") == "0 0 0 ? * 1 *"
    assert _quartz_cron("daily") == "0 0 0 * * ? *"


def test_build_dependent_task():
    from cli_anything.dolphinscheduler.core.tasks import build_dependent_task
    task = build_dependent_task(
        name="wait_upstream_dwd_sale",
        depend_items=[{
            "depTaskType": "DEP_WORKFLOW",
            "projectCode": "12345",
            "definitionCode": "67890",
            "depCycle": "DAY",
            "dateValue": "today",
        }],
    )
    assert task.task_type == "DEPENDENT"
    dep = task.task_params["dependence"]
    assert dep["relation"] == "AND"
    # DS DependentParameters shape: dependence.dependTaskList[].dependItemList[]
    # (a bare dependItemList is rejected with "parameter invalid").
    assert dep["dependTaskList"][0]["relation"] == "AND"
    assert dep["dependTaskList"][0]["dependItemList"][0]["definitionCode"] == "67890"


def test_warehouse_layers_helper():
    from wren_agent.nodes.deployment import _warehouse_layers
    state = create_initial_state("t", "goal", [_warehouse_source()], {},
                                 source_mode="warehouse")
    layers = _warehouse_layers(state)
    assert layers["dws"] == "db_dws"
    assert layers["ads"] == "db_ads"



# ── ADS prompt narrowing (tasks a1af4bed / c0f31ae0) ──


def test_ads_upstream_serialization_narrows_to_referenced_dws():
    """ADS design must receive ONLY the dws explored tables this plan's DWS
    references + the plan's own DWS designs — no dim/dwd layers, no unreferenced
    dws tables. The full dump bloated the ADS prompt past the LLM timeout."""
    from wren_agent.models.schema import FieldSchema as _FS
    from wren_agent.nodes.design_layers import _serialize_upstream_designs

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dim": SchemaExploration(
            source_db_id="wh#dim",
            tables=[TableSchema(
                name="dim_building", schema_name="dim_ecommerce_shop_sh",
                db_type="doris",
                fields=[_FS(name="member_bk", data_type="VARCHAR(64)")],
            )],
        ),
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[
                TableSchema(
                    name="dws_source_a", schema_name="dws_ecommerce_orders_sh",
                    db_type="doris",
                    fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
                ),
                TableSchema(
                    name="dws_source_b", schema_name="dws_ecommerce_orders_sh",
                    db_type="doris",
                    fields=[_FS(name="qty", data_type="BIGINT")],
                ),
            ],
        ),
    }
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="a", summary_table="dws_new_a",
        source_dwd_table="dws_ecommerce_orders_sh.dws_source_a",
        source_tables=["dws_ecommerce_orders_sh.dws_source_a"],
        fields=[{"name": "amount", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "amount", "expression": "SUM(amount)"}],
        group_by_dimensions=["stat_month"], grain=["stat_month"],
        requirement_coverage=[{
            "requirement": "amount", "status": "supported",
            "evidence": ["amount"], "gap_reason": "",
        }],
    )]
    state["design_plan"] = plan

    text = _serialize_upstream_designs(state, "ADS")

    # no dim layer at all
    assert "layer dim" not in text and "dim_building" not in text
    # referenced dws table present WITH its column detail
    assert "dws_source_a" in text and "amount" in text
    # unreferenced dws table excluded entirely (names AND detail)
    assert "dws_source_b" not in text and "qty" not in text
    # this plan's DWS layer still appended
    assert "This Plan's DWS Layer" in text and "dws_new_a" in text



# ── ADS prompt drops the cross-layer exploration dump (task c0f31ae0) ──


def test_ads_warehouse_prompt_drops_exploration_dump():
    from wren_agent.models.schema import FieldSchema as _FS

    """Warehouse ADS must NOT receive `## Source Schema Explorations` — its
    only upstream context is the narrowed dws section + this plan's DWS
    designs. The 80-table/400K-char dump pushed the ADS call to timeout."""
    from wren_agent.nodes.design_layers import _build_layer_context

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dim": SchemaExploration(
            source_db_id="wh#dim",
            tables=[TableSchema(
                name="dim_building", schema_name="dim_ecommerce_shop_sh",
                db_type="doris",
                fields=[_FS(name="member_bk", data_type="VARCHAR(64)")],
            )],
        ),
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[TableSchema(
                name="dws_source_a", schema_name="dws_ecommerce_orders_sh",
                db_type="doris",
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
    }
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="a", summary_table="dws_new_a",
        source_dwd_table="dws_ecommerce_orders_sh.dws_source_a",
        source_tables=["dws_ecommerce_orders_sh.dws_source_a"],
        fields=[{"name": "amount", "type": "DECIMAL(20,2)"}],
        measures=[{"name": "amount", "expression": "SUM(amount)"}],
        group_by_dimensions=["stat_month"], grain=["stat_month"],
        requirement_coverage=[{
            "requirement": "amount", "status": "supported",
            "evidence": ["amount"], "gap_reason": "",
        }],
    )]
    state["design_plan"] = plan

    ctx = _build_layer_context(state, "ADS")
    msg = ctx["user_message"]

    assert "## Source Schema Explorations" not in msg
    assert "dim_building" not in msg
    # narrowed upstream context still present
    assert "This Plan's DWS Layer" in msg
    assert "dws_new_a" in msg
    assert "dws_source_a" in msg


def test_dws_warehouse_prompt_keeps_exploration_dump():
    from wren_agent.models.schema import FieldSchema as _FS

    """DWS (and business-mode ADS) still need the exploration dump — only
    warehouse ADS drops it."""
    from wren_agent.nodes.design_layers import _build_layer_context

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dim": SchemaExploration(
            source_db_id="wh#dim",
            tables=[TableSchema(
                name="dim_building", schema_name="dim_ecommerce_shop_sh",
                db_type="doris",
                fields=[_FS(name="member_bk", data_type="VARCHAR(64)")],
            )],
        ),
    }
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")

    ctx = _build_layer_context(state, "DWS")
    assert "## Source Schema Explorations" in ctx["user_message"]
    assert "dim_building" in ctx["user_message"]

    # business-mode ADS keeps it too (unchanged behavior)
    biz = create_initial_state("t", "goal", [], {})
    biz["explorations"] = {
        "db#business": SchemaExploration(
            source_db_id="db#business",
            tables=[TableSchema(
                name="fct_rent", schema_name="", db_type="mysql",
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
    }
    biz["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")
    ctx2 = _build_layer_context(biz, "ADS")
    assert "## Source Schema Explorations" in ctx2["user_message"]



# ── Prompt dedup: DWS upstream empty + grill sampling (task c0f31ae0) ──


def test_dws_warehouse_upstream_is_empty():
    from wren_agent.models.schema import FieldSchema as _FS

    """Warehouse DWS upstream must NOT re-dump the explored tables — the
    `## Source Schema Explorations` section already carries them."""
    from wren_agent.nodes.design_layers import _serialize_upstream_designs

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["explorations"] = {
        "wh#dws": SchemaExploration(
            source_db_id="wh#dws",
            tables=[TableSchema(
                name="dws_source_a", schema_name="dws_ecommerce_orders_sh",
                db_type="doris",
                fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
            )],
        ),
    }
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")

    text = _serialize_upstream_designs(state, "DWS")
    assert "dws_source_a" not in text
    assert "Existing warehouse tables" not in text


def test_grill_exploration_summary_is_sampled():
    from wren_agent.models.schema import FieldSchema as _FS

    """The grill prompt must sample tables by goal relevance instead of
    dumping every table (c0f31ae0: 100K+ chars)."""
    from wren_agent.nodes.grill import _build_exploration_summary

    state = create_initial_state("t", "出租店铺应收", [], {})
    tables = [
        TableSchema(
            name=f"dws_rent_{i:02d}", schema_name="dws", db_type="doris",
            fields=[_FS(name="amount", data_type="DECIMAL(20,2)")],
        )
        for i in range(60)
    ] + [
        TableSchema(
            name=f"dim_unrelated_{i:02d}", schema_name="dim", db_type="doris",
            fields=[_FS(name="id", data_type="BIGINT")],
        )
        for i in range(40)
    ]
    state["explorations"] = {"db#dws": SchemaExploration(
        source_db_id="db#dws", tables=tables,
    )}

    text = _build_exploration_summary(state)

    assert "dws_rent_00" in text  # goal-relevant table kept
    assert len(text) < 60_000
    assert "omitted (sampled)" in text



# ── 0-row tables excluded from design context (task 432c17b1) ──


def test_exploration_serialization_excludes_empty_tables():
    from wren_agent.models.schema import FieldSchema as _FS

    """0-row tables must not appear in the design prompt at all — the LLM
    kept anchoring on empty-but-well-named tables despite the [EMPTY] mark."""
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
    text = _serialize_explorations_for_prompt(state)
    assert "dws_empty_table" not in text
    assert "dws_full_table" in text



# ── Large-design output batching (task 432c17b1) ──


@pytest.mark.asyncio
async def test_generate_layer_designs_batches_large_plans(monkeypatch):
    """>8 planned tables must split into batches of 5, merge results, and
    dedup designs the LLM repeats from earlier batches."""
    from wren_agent.nodes import design_layers as dl

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")

    plan_tables = [f"dws_t{i:02d}" for i in range(12)]
    batch_calls: list[list[str]] = []

    async def fake_plan(state, layer):
        return plan_tables, {}

    async def fake_batch(state, layer, batch, done, retry=False, context=None):
        batch_calls.append(list(batch))
        # every batch returns its tables + one drifted repeat of an earlier one
        return {
            "dws_designs": [
                {"summary_table": name, "grain": ["g1"], "fields": [],
                 "measures": [], "requirement_coverage": [], "ddl": "x", "load_sql": "y"}
                for name in batch
            ] + ([{"summary_table": "dws_t00", "grain": ["g1"]}] if done else []),
            "warnings": [f"warn-{batch[0]}"],
        }

    monkeypatch.setattr(dl, "_plan_layer_tables", fake_plan)
    monkeypatch.setattr(dl, "_generate_layer_batch", fake_batch)

    merged = await dl._generate_layer_designs(state, "DWS")

    assert batch_calls == [
        ["dws_t00", "dws_t01", "dws_t02", "dws_t03", "dws_t04"],
        ["dws_t05", "dws_t06", "dws_t07", "dws_t08", "dws_t09"],
        ["dws_t10", "dws_t11"],
    ]
    names = [d["summary_table"] for d in merged["dws_designs"]]
    assert len(names) == 12 and len(set(names)) == 12  # drift deduped
    assert "dws_t00" in names and "dws_t11" in names
    assert len(merged["warnings"]) == 3


@pytest.mark.asyncio
async def test_generate_layer_designs_single_call_for_small_plans(monkeypatch):
    """≤8 planned tables keep the single-call path."""
    from wren_agent.nodes import design_layers as dl

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")

    async def fake_plan(state, layer):
        return ["dws_a", "dws_b"], {}

    async def fake_single(state, layer):
        return {"dws_designs": [{"summary_table": "dws_a"}], "warnings": []}

    monkeypatch.setattr(dl, "_plan_layer_tables", fake_plan)
    monkeypatch.setattr(dl, "_llm_generate_layer", fake_single)

    merged = await dl._generate_layer_designs(state, "DWS")
    assert merged["dws_designs"] == [{"summary_table": "dws_a"}]
