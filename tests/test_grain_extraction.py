"""Tests for programmatic grain-combination extraction (task d7418ecc).

The DWS prompt now receives an explicit "Required Grain Combinations" checklist
computed from the modeling goal — the LLM must not infer coverage depth from
free text on its own (thin DWS tables repeatedly failed to cover the requested
单位×店铺×商品 × 月/季 analysis).
"""

from __future__ import annotations

import pytest

from wren_agent.nodes.design_layers import (
    _extract_required_grains,
    _serialize_required_grains,
)
from wren_agent.state import create_initial_state

GOAL = (
    "对销售订单的应收实收进行建模分析，设置不少于5个原子指标与10个派生指标，"
    "详尽的描述各个单位、各个月、季度的店铺、商品等收款的深度分析情况"
)


def test_extract_required_grains_covers_entity_time_combos():
    grains = _extract_required_grains(GOAL)
    # 单位/店铺/商品 × 月/季 — synonyms normalized (单位=组织, 月=月度/按月, 季=季度)
    for combo in ("单位×月", "单位×季", "店铺×月", "店铺×季", "商品×月", "商品×季"):
        assert combo in grains, combo
    # No duplicate synonyms
    assert len(grains) == len(set(grains))
    assert grains.count("单位×月") == 1


def test_extract_required_grains_adds_drilldown_chain():
    grains = _extract_required_grains(GOAL)
    # 三级穿透组合
    assert "单位×店铺×商品×月" in grains


def test_extract_required_grains_empty_for_plain_goals():
    assert _extract_required_grains("统计每日交易金额") == []
    assert _extract_required_grains("") == []


def test_serialize_required_grains_injected_only_for_dws_ads():
    state = create_initial_state("t", GOAL, [], {})
    dws = _serialize_required_grains(state, "DWS")
    assert "单位×月" in dws
    assert "MANDATORY coverage checklist" in dws
    assert _serialize_required_grains(state, "ODS") == ""
    assert _serialize_required_grains(state, "DIM") == ""


def test_serialize_required_grains_multi_entity_hard_rule():
    """task 8e76b357: the design LLM built single-entity tables only and marked
    the JOINed `单位×店铺×月` unsupported. The checklist must now carry a HARD
    REQUIREMENT section for multi-entity grains."""
    from wren_agent.nodes.design_layers import _grain_entity_count

    assert _grain_entity_count("单位×店铺×月") == 2
    assert _grain_entity_count("单位×月") == 1
    assert _grain_entity_count("店铺×季") == 1
    assert _grain_entity_count("单位×店铺×商品×月") == 3

    state = create_initial_state("t", GOAL, [], {})
    state["required_grains"] = ["单位×月", "单位×店铺×月", "单位×店铺×商品×季"]
    dws = _serialize_required_grains(state, "DWS")
    assert "Multi-entity grains (HARD REQUIREMENT" in dws
    assert "单位×店铺×月" in dws
    # must appear in the affected list
    assert "Affected: " in dws


def test_serialize_required_grains_prefers_llm_extracted():
    """LLM-extracted grains (cached on state) win over keyword extraction —
    the LLM understands nesting and filters topic nouns like 合同."""
    state = create_initial_state("t", GOAL, [], {})
    state["required_grains"] = ["单位×月", "单位×季", "店铺×商品×月"]
    dws = _serialize_required_grains(state, "DWS")
    assert "单位×月" in dws
    assert "店铺×商品×月" in dws
    # keyword fallback would add 合同×月 — LLM result must not include it
    assert "合同×月" not in dws


@pytest.mark.asyncio
async def test_llm_extract_grains_parses_response(monkeypatch):
    from wren_agent.nodes.design_layers import _llm_extract_grains

    class FakeLLM:
        async def generate_json(self, system_prompt, user_message, token_kind):
            assert "合同" in user_message  # prompt carries the goal
            return '{"grains": ["单位×月", "单位×季"], "drilldowns": ["单位×店铺×商品×月"]}'

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client", lambda: FakeLLM()
    )
    state = create_initial_state("t", GOAL, [], {})
    grains = await _llm_extract_grains(state)
    assert grains == ["单位×月", "单位×季", "单位×店铺×商品×月"]


@pytest.mark.asyncio
async def test_llm_extract_grains_falls_back_on_failure(monkeypatch):
    from wren_agent.nodes.design_layers import _llm_extract_grains

    class BoomLLM:
        async def generate_json(self, system_prompt, user_message, token_kind):
            raise RuntimeError("llm down")

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client", lambda: BoomLLM()
    )
    state = create_initial_state("t", GOAL, [], {})
    assert await _llm_extract_grains(state) is None
