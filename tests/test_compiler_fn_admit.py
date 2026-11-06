"""Task-scoped compiler function admit (ADR-0018).

Off-whitelist Doris scalars (GREATEST) may be admitted for THIS task after
EXPLAIN succeeds. Deny-list / known-agg / cap must not admit. Permanent
SCALAR_FUNCTIONS is unchanged.
"""

from __future__ import annotations

import pytest

from wren_agent.compiler.render import compile_semantic_design, try_compile_semantic_design
from wren_agent.models.semantic import SemanticDesign


def _surplus_design() -> SemanticDesign:
    return SemanticDesign(
        layer="DWS",
        summary_name="盘点盈亏月汇总",
        summary_table="dws_stock_inventory_monthly",
        columns=[
            {"name": "stat_month", "dtype": "VARCHAR(7)", "comment": "统计月",
             "is_key": True, "source_expr": "DATE_FORMAT(a.inventory_date, '%Y-%m')"},
            {"name": "surplus_qty", "dtype": "DECIMAL(20,2)", "comment": "盘盈数量",
             "is_measure": True,
             "source_expr": "SUM(GREATEST(a.after_number - a.before_number, 0))"},
        ],
        sources=["dwd_stock_inventory_info"],
        requirement_coverage=[{
            "requirement": "盘盈",
            "status": "supported",
            "evidence": ["surplus_qty"],
        }],
    )


def test_greatest_refused_without_extra_functions():
    compiled, problems = try_compile_semantic_design(_surplus_design())
    assert compiled is None
    assert any("GREATEST" in p and "whitelist" in p for p in problems)


def test_greatest_compiles_with_task_extra_functions():
    out = compile_semantic_design(
        _surplus_design(), extra_functions=["GREATEST"],
    )
    assert out is not None
    assert "GREATEST(a.after_number - a.before_number, 0)" in out.load_sql


def test_permanent_whitelist_unchanged_by_extra_functions():
    from wren_agent.compiler.rules import SCALAR_FUNCTIONS
    compile_semantic_design(_surplus_design(), extra_functions=["GREATEST"])
    assert "GREATEST" not in SCALAR_FUNCTIONS


# ── admit tool (pure helpers) ───────────────────────────────────────


def test_unknown_functions_from_problems():
    from wren_agent.tools.compiler_fn_admit import unknown_functions_from_problems
    names = unknown_functions_from_problems([
        "column `surplus_qty`: function GREATEST() not on the compile whitelist "
        "('SUM(GREATEST(a.after_number - a.before_number, 0))')",
        "column `x`: empty expression",
    ])
    assert names == ["GREATEST"]


def test_stub_expr_replaces_qualified_columns_not_functions():
    from wren_agent.tools.compiler_fn_admit import stub_expr_for_explain
    stub = stub_expr_for_explain(
        "SUM(GREATEST(a.after_number - a.before_number, 0))",
    )
    assert "GREATEST" in stub
    assert "SUM" in stub
    assert "after_number" not in stub
    assert "1" in stub


def test_classify_deny_and_agg():
    from wren_agent.tools.compiler_fn_admit import classify_admit_name
    assert classify_admit_name("SLEEP") == "deny"
    assert classify_admit_name("RAND") == "deny"
    assert classify_admit_name("GROUP_CONCAT") == "agg"
    assert classify_admit_name("GREATEST") == "ok"
    assert classify_admit_name("not a fn!") == "invalid"


@pytest.mark.asyncio
async def test_admit_greatest_when_explain_ok():
    from wren_agent.state import create_initial_state
    from wren_agent.tools.compiler_fn_admit import try_admit_unknown_functions

    state = create_initial_state("t-admit", "goal", [], {})
    problems = [
        "column `surplus_qty`: function GREATEST() not on the compile whitelist ('SUM(GREATEST(x, 0))')",
    ]

    async def explain(sql: str):
        assert sql.upper().startswith("EXPLAIN")
        assert "GREATEST" in sql.upper()
        return True, ""

    outcome = await try_admit_unknown_functions(
        state, _surplus_design(), problems, explain=explain,
    )
    assert outcome.admitted == ["GREATEST"]
    assert state["compiler_extra_functions"] == ["GREATEST"]


@pytest.mark.asyncio
async def test_admit_skips_when_explain_fails():
    from wren_agent.state import create_initial_state
    from wren_agent.tools.compiler_fn_admit import try_admit_unknown_functions

    state = create_initial_state("t-admit", "goal", [], {})
    problems = [
        "column `surplus_qty`: function GREATEST() not on the compile whitelist ('SUM(GREATEST(x, 0))')",
    ]

    async def explain(_sql: str):
        return False, "Unknown function 'GREATEST'"

    outcome = await try_admit_unknown_functions(
        state, _surplus_design(), problems, explain=explain,
    )
    assert outcome.admitted == []
    assert "GREATEST" not in (state.get("compiler_extra_functions") or [])
    assert "Unknown function" in outcome.explain_error


@pytest.mark.asyncio
async def test_admit_never_adds_deny_list_even_if_explain_ok():
    from wren_agent.state import create_initial_state
    from wren_agent.tools.compiler_fn_admit import try_admit_unknown_functions

    state = create_initial_state("t-admit", "goal", [], {})
    problems = [
        "column `x`: function SLEEP() not on the compile whitelist ('SLEEP(1)')",
    ]

    async def explain(_sql: str):
        return True, ""

    design = _surplus_design()
    design.columns[-1].source_expr = "SLEEP(1)"
    outcome = await try_admit_unknown_functions(
        state, design, problems, explain=explain,
    )
    assert outcome.admitted == []
    assert "SLEEP" in outcome.skipped
    assert not state.get("compiler_extra_functions")


@pytest.mark.asyncio
async def test_admit_respects_per_task_cap():
    from wren_agent.tools.compiler_fn_admit import EXTRA_FN_CAP, try_admit_unknown_functions
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-admit", "goal", [], {})
    state["compiler_extra_functions"] = [f"FN{i}" for i in range(EXTRA_FN_CAP)]
    problems = [
        "column `surplus_qty`: function GREATEST() not on the compile whitelist ('SUM(GREATEST(x, 0))')",
    ]

    async def explain(_sql: str):
        return True, ""

    outcome = await try_admit_unknown_functions(
        state, _surplus_design(), problems, explain=explain,
    )
    assert outcome.admitted == []
    assert "GREATEST" in outcome.skipped
    assert len(state["compiler_extra_functions"]) == EXTRA_FN_CAP


@pytest.mark.asyncio
async def test_try_compiler_dws_admits_greatest_and_emits_sql(monkeypatch):
    from wren_agent.config import settings
    from wren_agent.nodes import design_layers as dl
    from wren_agent.state import create_initial_state

    monkeypatch.setattr(settings.llm, "compiler_enabled", True)

    async def explain(_sql: str):
        return True, ""

    monkeypatch.setattr(
        "wren_agent.tools.compiler_fn_admit.explain_select", explain,
    )

    state = create_initial_state("t-admit", "goal", [], {})
    used, designs, artifacts = await dl._try_compiler_dws(
        state, {"semantic_designs": [_surplus_design().model_dump()]},
    )
    assert used is True
    assert designs
    assert any(a.sql_type == "DML_INSERT" for a in artifacts)
    assert any("GREATEST" in a.sql for a in artifacts if a.sql_type == "DML_INSERT")
    assert "GREATEST" in (state.get("compiler_extra_functions") or [])
