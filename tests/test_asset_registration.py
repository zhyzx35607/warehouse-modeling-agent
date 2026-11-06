"""Approved-asset registration tests (metric_registry / dimension_registry).

register_metric/register_dimension previously had zero call sites — the
registries stayed empty and the ADS prompt's registry context was always
blank. _register_approved_assets runs on design-plan approval and must be
idempotent per task and non-fatal on registry errors.
"""

import pytest

from wren_agent.models.design import ADSDesign, DIMDesign, DWDesignPlan
from wren_agent.state import create_initial_state
from wren_agent.models.task import ApprovalItem, ApprovalStatus


class _FakePool:
    """Minimal pool: fetchval returns 0 (nothing registered yet)."""

    def __init__(self, registers):
        self.registers = registers

    def acquire(self):
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def fetchval(self, _sql, *_args):
        return 0


def _plan_with_assets() -> DWDesignPlan:
    plan = DWDesignPlan(task_id="t-reg", modeling_goal="goal")
    plan.ads_designs = [ADSDesign(
        application_name="a", ads_table="ads_receivable_month",
        source_dws_tables=["dws_receivable_month"],
        dimensions=["stat_month", "org_id"],
        fields=[{"name": "stat_month", "type": "VARCHAR(7)"}],
        metrics=[
            {"name": "应收金额", "metric_type": "atomic",
             "expression": "SUM(monthly_amount_receivable)"},
            {"name": "支付率", "metric_type": "derived",
             "parent_metrics": ["monthly_amount_receivable", "monthly_amount_paid"],
             "expression": "paid / receivable"},
        ],
    )]
    plan.dim_designs = [DIMDesign(
        dim_name="商品维", dim_table="dim_room", source="from_source_table",
        source_table="t_room", natural_key=["source_room_id"],
        fields=[{"name": "room_id", "comment": "商品ID"}],
    )]
    return plan


@pytest.mark.asyncio
async def test_approval_registers_metrics_and_dimensions(monkeypatch):
    from wren_agent.nodes.approval import _register_approved_assets

    calls = {"metrics": [], "dims": []}
    registered = {"status": "created", "metric_id": "m1"}

    async def fake_register_metric(**kwargs):
        calls["metrics"].append(kwargs)
        return registered

    async def fake_register_dimension(**kwargs):
        calls["dims"].append(kwargs)
        return registered

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: _FakePool(None))
    monkeypatch.setattr(
        "wren_agent.nodes.approval._register_approved_assets", None)  # noqa
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_metric", fake_register_metric)
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", fake_register_dimension)

    state = create_initial_state("t-reg", "goal", [], {}, source_mode="business_db")
    state["design_plan"] = _plan_with_assets()

    await _register_approved_assets(state)

    assert len(calls["metrics"]) == 2
    assert calls["metrics"][0]["metric_name"] == "应收金额"
    assert calls["metrics"][0]["owner_task_id"] == "t-reg"
    assert calls["metrics"][0]["source_table"] == "dws_receivable_month"
    assert calls["metrics"][0]["grain"] == "stat_month,org_id"
    assert calls["metrics"][1]["metric_type"] == "derived"
    assert calls["metrics"][1]["parent_metrics"] == [
        "monthly_amount_receivable", "monthly_amount_paid"]
    assert len(calls["dims"]) == 1
    assert calls["dims"][0]["dim_name"] == "商品维"
    assert calls["dims"][0]["natural_keys"] == ["source_room_id"]
    # business mode: no warehouse dim landing db → db_name stays ''
    assert calls["dims"][0]["db_name"] == ""
    assert any(
        a.get("action") == "registered_assets" and a.get("count") == 3
        for a in state.get("audit_log", [])
    )


@pytest.mark.asyncio
async def test_approval_registers_dimension_with_warehouse_db(monkeypatch):
    """Warehouse mode: DIM registration carries the dim layer landing db."""
    from wren_agent.nodes.approval import _register_approved_assets

    calls = {"metrics": [], "dims": []}

    async def fake_register_metric(**kwargs):
        calls["metrics"].append(kwargs)
        return {"status": "created", "metric_id": "m1"}

    async def fake_register_dimension(**kwargs):
        calls["dims"].append(kwargs)
        return {"status": "created", "dim_id": "d1"}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: _FakePool(None))
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_metric", fake_register_metric)
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", fake_register_dimension)
    monkeypatch.setattr(
        "wren_agent.nodes.deployment._warehouse_layers",
        lambda state: {"dim": "db_dim", "dws": "dws_x"},
    )

    state = create_initial_state("t-wh", "goal", [], {}, source_mode="warehouse")
    state["design_plan"] = _plan_with_assets()

    await _register_approved_assets(state)

    assert len(calls["dims"]) == 1
    assert calls["dims"][0]["db_name"] == "db_dim"


@pytest.mark.asyncio
async def test_approval_registration_failure_only_warns(monkeypatch):
    """Registry errors must never block deployment — warn and continue."""
    from wren_agent.nodes.approval import _register_approved_assets

    async def boom(**kwargs):
        raise RuntimeError("pg_trgm not available")

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: _FakePool(None))
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_metric", boom)
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", boom)

    state = create_initial_state("t-reg", "goal", [], {}, source_mode="business_db")
    state["design_plan"] = _plan_with_assets()

    await _register_approved_assets(state)

    assert any("Asset registration skipped" in w for w in state.get("warnings", []))
    assert not state.get("errors")


def test_bootstrap_metric_rows_from_columns():
    """Generic column-dict input → metric rows (only numeric, non-id columns)."""
    from wren_agent.tools.metric_bootstrap import _bootstrap_metric_rows_from_columns

    columns = [
        {"source_table": "db_dws.dws_test", "column_name": "amount",
         "data_type": "DECIMAL(19,4)", "comment": "金额",
         "pk_names": ["stat_month"], "layer": "dws"},
        {"source_table": "db_dws.dws_test", "column_name": "source_record_id",
         "data_type": "BIGINT", "comment": "源记录ID", "pk_names": [], "layer": "dws"},
        {"source_table": "db_dws.dws_test", "column_name": "name",
         "data_type": "VARCHAR(200)", "comment": "名称", "pk_names": [], "layer": "dws"},
        {"source_table": "db_dws.dws_test", "column_name": "receipt_count",
         "data_type": "BIGINT", "comment": "收款笔数", "pk_names": [], "layer": "dws"},
    ]
    rows = _bootstrap_metric_rows_from_columns(columns)
    assert [r["metric_name"] for r in rows] == ["金额", "收款笔数"]
    assert rows[0]["definition_sql"] == "SUM(`amount`)"
    assert rows[0]["grain"] == "stat_month"
    assert rows[0]["layer"] == "dws"


@pytest.mark.asyncio
async def test_process_approval_triggers_registration(monkeypatch):
    from wren_agent.nodes.approval import process_approval

    triggered = []

    async def fake_register(state):
        triggered.append(state["task_id"])

    monkeypatch.setattr(
        "wren_agent.nodes.approval._register_approved_assets", fake_register)

    state = create_initial_state("t-reg2", "goal", [], {}, source_mode="business_db")
    state["design_plan"] = _plan_with_assets()
    state["approval_items"] = [ApprovalItem(
        artifact_type="design_plan", artifact_id="p1", title="方案",
        description="d", status=ApprovalStatus.APPROVED,
    )]

    await process_approval(state)

    assert triggered == ["t-reg2"]
    assert state["design_plan"].approved is True
