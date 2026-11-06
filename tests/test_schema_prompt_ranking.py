"""Regression tests for goal-relevance schema prompt ranking (task 4e372c61).

The design prompt used to take the first N explored tables in exploration
order — with a 374-table warehouse that meant 20 dim tables and ZERO DWD
facts, the LLM could not design the requested model and the fallback
produced unrelated tables.
"""

from __future__ import annotations

from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.nodes.design_layers import (
    _MAX_SCHEMA_PROMPT_CHARS,
    _MIN_TABLES_PER_LAYER,
    _goal_keywords,
    _select_relevant_tables,
    _serialize_explorations_for_prompt,
)
from wren_agent.state import create_initial_state

GOAL = "对销售订单的应收实收进行建模分析，设置不少于5个原子指标与10个派生指标，详尽的描述各个单位、各个月、季度的店铺、商品等收款的深度分析情况"


def _warehouse_explorations(n_dims: int = 20, n_dwds: int = 25) -> dict[str, SchemaExploration]:
    dim_tables = [
        TableSchema(
            name=f"dim_aux_{i}", schema_name="dim_ecommerce_shop_bj",
            db_type="doris",
            fields=[FieldSchema(name=f"code_{i}", data_type="VARCHAR(60)")],
        )
        for i in range(n_dims)
    ]
    dim_tables += [
        TableSchema(
            name="dim_order_shop", schema_name="dim_ecommerce_shop_bj",
            db_type="doris",
            fields=[
                FieldSchema(name="dim_sk", data_type="BIGINT"),
                FieldSchema(name="house_name", data_type="VARCHAR(255)"),
            ],
        ),
    ]
    dwd_tables = [
        TableSchema(
            name=f"dwd_aux_flow_{i}", schema_name="dwd_ecommerce_orders_BJ",
            db_type="doris",
            fields=[
                FieldSchema(name="stat_date", data_type="DATE"),
                FieldSchema(name="amount", data_type="DECIMAL(20,2)"),
            ],
        )
        for i in range(n_dwds)
    ]
    dwd_tables += [
        TableSchema(
            name="fct_order_outbound_receivable_cycle_acc",
            schema_name="dwd_ecommerce_orders_BJ", db_type="doris",
            fields=[
                FieldSchema(name="cycle_start_date", data_type="DATE"),
                FieldSchema(name="amount_receivable", data_type="DECIMAL(20,2)"),
                FieldSchema(name="cumulative_paid_amount", data_type="DECIMAL(20,2)"),
                FieldSchema(name="basics_room_id", data_type="BIGINT"),
            ],
        ),
    ]
    return {
        "wh#dim": SchemaExploration(source_db_id="wh#dim", tables=dim_tables),
        "wh#dwd": SchemaExploration(source_db_id="wh#dwd", tables=dwd_tables),
    }


def test_relevant_fact_tables_rank_above_first_explored_dims():
    state = create_initial_state("t", GOAL, [], {})
    state["explorations"] = _warehouse_explorations()

    selected = _select_relevant_tables(
        state["explorations"], _goal_keywords(GOAL), 20,
    )
    names = [t.name for _db, t in selected]

    # The receivable-cycle fact must be selected (it was entirely absent from
    # the pre-fix prompt), and within its layer it must rank above the
    # alphabetically-first filler tables — the per-layer floor (dim join
    # context) comes first by design.
    fact = "fct_order_outbound_receivable_cycle_acc"
    assert fact in names
    assert names.index(fact) < names.index("dwd_aux_flow_0")


def test_per_layer_floor_keeps_dim_join_context():
    state = create_initial_state("t", GOAL, [], {})
    state["explorations"] = _warehouse_explorations()

    selected = _select_relevant_tables(
        state["explorations"], _goal_keywords(GOAL), 20,
    )
    from collections import Counter
    layers = Counter(db_id.rsplit("#", 1)[-1] for db_id, _t in selected)
    # Both explored layers keep at least the floor (facts outrank dims).
    assert layers["dim"] >= _MIN_TABLES_PER_LAYER
    assert layers["dwd"] >= _MIN_TABLES_PER_LAYER


def test_serialized_prompt_respects_character_budget():
    state = create_initial_state("t", GOAL, [], {})
    state["explorations"] = _warehouse_explorations()

    prompt = _serialize_explorations_for_prompt(state)
    assert len(prompt) <= _MAX_SCHEMA_PROMPT_CHARS + 2000
    # The relevant fact is present with its columns (pre-fix it was absent).
    assert "fct_order_outbound_receivable_cycle_acc" in prompt
    assert "amount_receivable" in prompt


def test_huge_exploration_is_capped_by_budget():
    """Over 80 tables (the max_tables cap) still emits the omitted marker and
    stays under the character budget — but the fact tables rank first."""
    state = create_initial_state("t", GOAL, [], {})
    state["explorations"] = _warehouse_explorations(n_dims=90, n_dwds=90)

    prompt = _serialize_explorations_for_prompt(state)
    assert len(prompt) <= _MAX_SCHEMA_PROMPT_CHARS + 2000
    assert "omitted for prompt size" in prompt
    assert "fct_order_outbound_receivable_cycle_acc" in prompt


def test_goal_keywords_map_chinese_terms_to_english_tokens():
    kws = _goal_keywords(GOAL)
    assert "receivable" in kws
    assert "order" in kws
    assert "product" in kws
    assert "paid" in kws


def test_small_exploration_is_not_truncated():
    """A tiny exploration (like the old 77-table business_db cases) still gets
    every table — ranking must never shrink what used to fit."""
    state = create_initial_state("t", "goal", [], {})
    state["explorations"] = {
        "db#dwd": SchemaExploration(source_db_id="db#dwd", tables=[
            TableSchema(
                name="fct_payment", schema_name="db", db_type="doris",
                fields=[FieldSchema(name="amount", data_type="DECIMAL(20,2)")],
            ),
            TableSchema(
                name="dim_org", schema_name="db", db_type="doris",
                fields=[FieldSchema(name="org_id", data_type="BIGINT")],
            ),
        ]),
    }
    prompt = _serialize_explorations_for_prompt(state)
    assert "fct_payment" in prompt
    assert "dim_org" in prompt
