"""Database-scoped registry filtering tests.

task_relevant_db_names computes the databases a task touches; the registry
queries accept a db scope so design-layer injection never leaks OTHER
warehouse sources' metrics/dimensions into a task's prompt.
"""

import pytest

from wren_agent.models.schema import SchemaExploration, TableSchema
from wren_agent.state import create_initial_state
from wren_agent.tools.pg_store import query_dimension_registry, query_metric_registry


class _Pool:
    def __init__(self, fetch_rows=None):
        self.fetch_rows = fetch_rows or []
        self.executed = []

    def acquire(self):
        return _Conn(self)


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def fetch(self, sql, *_args):
        self.pool.executed.append(("fetch", sql, _args))
        return self.pool.fetch_rows


# ── task_relevant_db_names ──────────────────────────────────────


def test_task_relevant_db_names_business_mode_empty():
    from wren_agent.nodes.deployment import task_relevant_db_names

    state = create_initial_state("t", "goal", [], {}, source_mode="business_db")
    assert task_relevant_db_names(state) == set()


def test_task_relevant_db_names_warehouse_landing_and_explored():
    from wren_agent.nodes.deployment import task_relevant_db_names

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [{
        "connection_params": {"layers": {"dws": ["dws_ecommerce_orders_sh"],
                                         "ads": ["ads_ecommerce_SH"]}},
    }]
    exp = SchemaExploration(source_db_id="w#dws")
    exp.tables.append(TableSchema(
        name="dws_rental_month", schema_name="dws_ecommerce_orders_sh", db_type="doris"))
    exp.tables.append(TableSchema(
        name="dws_other", schema_name="dws_ecommerce_orders_XYZ", db_type="doris"))
    state["explorations"] = {"w#dws": exp}

    dbs = task_relevant_db_names(state)
    # original casing preserved — the query layer lower()s for matching
    assert dbs == {"dws_ecommerce_orders_sh", "dws_ecommerce_orders_XYZ",
                   "ads_ecommerce_SH"}


# ── query_metric_registry(db_names=...) ─────────────────────────


def _metric_row(**overrides):
    row = {"metric_id": "m1", "metric_name": "应收金额", "metric_type": "atomic",
           "definition_sql": "SUM(x)", "source_table": "dws_ecommerce_orders_sh.dws_x",
           "owner_task_id": "t1", "grain": "stat_month",
           "parent_metrics": [], "tags": [], "active": True}
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_query_metric_registry_db_filter(monkeypatch):
    """db_names scope binds a case-insensitive database filter on split_part."""
    pool = _Pool(fetch_rows=[_metric_row()])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    out = await query_metric_registry(db_names=["dws_ecommerce_orders_sh"])
    assert len(out) == 1
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "lower(split_part(source_table, '.', 1)) = ANY($1::text[])" in sql
    assert tuple(args) == (["dws_ecommerce_orders_sh"],)  # lowered for matching


@pytest.mark.asyncio
async def test_query_metric_registry_no_db_filter(monkeypatch):
    """No db_names → no database filter (pre-filter behavior)."""
    pool = _Pool(fetch_rows=[_metric_row()])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    await query_metric_registry()
    sql = pool.executed[0][1]
    assert "split_part" not in sql


@pytest.mark.asyncio
async def test_query_metric_registry_name_and_db(monkeypatch):
    """Name search and db filter combine with parameterized placeholders."""
    pool = _Pool(fetch_rows=[_metric_row()])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    await query_metric_registry(metric_name="应收", db_names=["db_a", "db_b"])
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "metric_name ILIKE $1" in sql
    assert "lower(split_part(source_table, '.', 1)) = ANY($2::text[])" in sql
    assert tuple(args) == ("%应收%", ["db_a", "db_b"])


@pytest.mark.asyncio
async def test_query_metric_registry_goal_filter(monkeypatch):
    """goal narrows to semantically related entries (mock filter_registry_by_goal)."""
    pool = _Pool(fetch_rows=[_metric_row(metric_name="应收金额"),
                             _metric_row(metric_name="无关指标")])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)

    async def fake_filter(db_names, goal, *, kind):
        assert kind == "metric"
        assert goal == "对销售订单的应收实收建模"
        return ["应收金额"]

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.filter_registry_by_goal", fake_filter)
    out = await query_metric_registry(
        db_names=["dws_ecommerce_orders_sh"], goal="对销售订单的应收实收建模")
    assert [r["metric_name"] for r in out] == ["应收金额"]


@pytest.mark.asyncio
async def test_query_metric_registry_goal_filter_unavailable_keeps_all(monkeypatch):
    """goal filter returning None (embedding unavailable) → keep everything."""
    pool = _Pool(fetch_rows=[_metric_row(metric_name="应收金额"),
                             _metric_row(metric_name="无关指标")])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)

    async def fake_unavailable(*a, **k):
        return None

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.filter_registry_by_goal", fake_unavailable)
    out = await query_metric_registry(goal="一些目标")
    assert len(out) == 2  # none dropped


# ── query_dimension_registry(db_names=...) ──────────────────────


def _dim_row(**overrides):
    row = {"dim_id": "d1", "dim_name": "商品维", "dim_table": "dim_room",
           "db_name": "db_dim", "owner_task_id": "t1",
           "natural_keys": ["source_room_id"], "grain": "source_room_id",
           "fields": [], "variant_of": None, "version": 1, "active": True}
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_query_dimension_registry_db_filter_keeps_bare(monkeypatch):
    """db_names scope matches the db (case-insensitive) AND keeps bare legacy."""
    pool = _Pool(fetch_rows=[_dim_row(), _dim_row(dim_table="dim_legacy", db_name="")])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    out = await query_dimension_registry(db_names=["DB_DIM"])
    assert len(out) == 2
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "lower(db_name) = ANY($1::text[]) OR db_name = ''" in sql
    assert tuple(args) == (["db_dim"],)  # lowered for matching


@pytest.mark.asyncio
async def test_query_dimension_registry_no_db_filter(monkeypatch):
    """No db_names → no filter clause."""
    pool = _Pool(fetch_rows=[_dim_row()])
    monkeypatch.setattr("wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    await query_dimension_registry()
    sql = pool.executed[0][1]
    assert "db_name" not in sql


# ── layer_relevant_db_names / registry_db_name (deployment) ──────


def _layer_exploration(schemas: list[str], key: str):
    exp = SchemaExploration(source_db_id=key)
    for s in schemas:
        exp.tables.append(TableSchema(name="t", schema_name=s, db_type="doris"))
    return exp


def test_layer_relevant_db_names_warehouse_dws_scope():
    from wren_agent.nodes.deployment import layer_relevant_db_names

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [{
        "connection_params": {"layers": {"dim": ["db_dim"], "dwd": ["db_dwd"],
                                         "dws": ["dws_x"]}},
    }]
    state["explorations"] = {
        "w#dim": _layer_exploration(["dim_ecommerce_shop_sh"], "w#dim"),
        "w#dwd": _layer_exploration(["dwd_ecommerce_orders_sh"], "w#dwd"),
        "w#dws": _layer_exploration(["dws_ecommerce_orders_sh"], "w#dws"),
    }
    # DWS design consumes dim∪dwd sources
    assert layer_relevant_db_names(state, {"dim", "dwd"}) == {
        "db_dim", "dim_ecommerce_shop_sh", "db_dwd", "dwd_ecommerce_orders_sh"}
    # ADS anchors on dws sources
    assert layer_relevant_db_names(state, {"dws"}) == {"dws_x", "dws_ecommerce_orders_sh"}
    # dws layer does not leak into dim scope
    assert "dws_x" not in layer_relevant_db_names(state, {"dim"})


def test_layer_relevant_db_names_business_empty():
    from wren_agent.nodes.deployment import layer_relevant_db_names

    state = create_initial_state("t", "goal", [], {}, source_mode="business_db")
    assert layer_relevant_db_names(state, {"dws"}) == set()


def test_registry_db_name_prefers_explored_schema():
    """dim_table with dim_ prefix resolves to the explored real schema."""
    from wren_agent.nodes.deployment import registry_db_name

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["source_databases"] = [{
        "connection_params": {"layers": {"dim": ["db_dim"]}},
    }]
    exp = SchemaExploration(source_db_id="w#dim")
    exp.tables.append(TableSchema(name="dim_room", schema_name="dim_ecommerce_shop_sh",
                                  db_type="doris"))
    state["explorations"] = {"w#dim": exp}
    # exact name match → real schema wins over configured db_dim
    assert registry_db_name(state, "dim_room", "dim") == "dim_ecommerce_shop_sh"
    # dim_-stripped source name matches explored table too
    assert registry_db_name(state, "dim_room", "dim") == "dim_ecommerce_shop_sh"
    # unknown table → configured dim layer db fallback
    assert registry_db_name(state, "dim_unknown", "dim") == "db_dim"


# ── _inject_registry_context layer scoping ──────────────────────


@pytest.mark.asyncio
async def test_inject_registry_context_dws_layer_scoping(monkeypatch):
    """DWS injection queries dim∪dwd dbs + goal; ADS queries dws dbs + goal."""
    from wren_agent.nodes import design_layers as dl
    from wren_agent.tools import pg_store as pg

    calls = []

    async def fake_dim_query(**kwargs):
        calls.append(("dim", kwargs))
        return [{"dim_name": "商品维", "dim_table": "dim_room", "grain": "room_id"}]

    async def fake_metric_query(**kwargs):
        calls.append(("metric", kwargs))
        return [{"metric_name": "应收金额", "metric_type": "atomic",
                 "source_table": "dws_x.t", "grain": "stat_month"}]

    monkeypatch.setattr(pg, "query_dimension_registry", fake_dim_query)
    monkeypatch.setattr(pg, "query_metric_registry", fake_metric_query)

    def fake_layer_dbs(state, layers):
        if layers == {"dim", "dwd"}:
            return {"db_dim", "dwd_x"}
        if layers == {"dws"}:
            return {"dws_x"}
        return set()

    monkeypatch.setattr(
        "wren_agent.nodes.deployment.layer_relevant_db_names", fake_layer_dbs)

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["modeling_goal"] = "对销售订单的应收实收建模"

    ctx = {"user_message": "", "system_prompt": ""}
    await dl._inject_registry_context(state, ctx, "DWS")
    # DWS asks for dimensions AND metrics scoped to dim∪dwd
    dim_call = next(kw for kind, kw in calls if kind == "dim")
    metric_call = next(kw for kind, kw in calls if kind == "metric")
    assert dim_call["db_names"] == ["db_dim", "dwd_x"]
    assert metric_call["db_names"] == ["db_dim", "dwd_x"]
    assert dim_call["goal"] == "对销售订单的应收实收建模"
    assert "Registered Dimensions" in ctx["user_message"]
    assert "Registered Metrics" in ctx["user_message"]
    assert "dim/dwd layer databases" in ctx["user_message"]

    # ADS asks for metrics scoped to dws
    calls.clear()
    ctx2 = {"user_message": "", "system_prompt": ""}
    await dl._inject_registry_context(state, ctx2, "ADS")
    metric_call2 = next(kw for kind, kw in calls if kind == "metric")
    assert metric_call2["db_names"] == ["dws_x"]
    assert "dws layer databases" in ctx2["user_message"]
