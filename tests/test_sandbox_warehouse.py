"""Sandbox validation: layer scope + warehouse DML skip."""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDesignPlan, SQLArtifact
from wren_agent.state import create_initial_state


@pytest.mark.asyncio
async def test_validate_design_plan_filters_by_layer(monkeypatch):
    from wren_agent.tools.sandbox_sql import SandboxSQL

    calls = []

    async def fake_ddl(self, sql, table_name="", drop=True):
        calls.append(("ddl", table_name))
        return {"valid": True, "table": table_name}

    async def fake_dml(self, sql, depends_tables=None):
        calls.append(("dml", sql[:40]))
        return {"valid": True, "explain_rows": 1}

    monkeypatch.setattr(SandboxSQL, "validate_ddl", fake_ddl)
    monkeypatch.setattr(SandboxSQL, "validate_dml", fake_dml)
    monkeypatch.setattr(SandboxSQL, "_ensure_sandbox_exists", lambda self: None)

    class _FakeDoris:
        async def execute(self, *_a, **_k):
            return None

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient", lambda database=None: _FakeDoris()
    )

    plan = DWDesignPlan(task_id="t", modeling_goal="g")
    plan.sql_artifacts = [
        SQLArtifact(layer="DWS", target_table="dws_x", sql_type="DDL", sql="CREATE TABLE dws_x(id INT)"),
        SQLArtifact(layer="DWS", target_table="dws_x", sql_type="DML_INSERT", sql="INSERT INTO dws_x SELECT 1"),
        SQLArtifact(layer="ADS", target_table="ads_x", sql_type="DDL", sql="CREATE TABLE ads_x(id INT)"),
        SQLArtifact(layer="ADS", target_table="ads_x", sql_type="DML_INSERT", sql="INSERT INTO ads_x SELECT 1"),
    ]

    sandbox = SandboxSQL()
    result = await sandbox.validate_design_plan(plan, layer="DWS", dml_mode="explain")
    assert result["total"] == 2
    assert [c[0] for c in calls] == ["ddl", "dml"]


@pytest.mark.asyncio
async def test_warehouse_skips_dml_sandbox(monkeypatch):
    from wren_agent.tools import sandbox_sql as mod

    async def fake_plan(self, plan, *, layer=None, dml_mode="explain"):
        assert layer == "DWS"
        assert dml_mode == "skip"
        return {
            "total": 2, "passed": 2, "failed": 0, "skipped": 1,
            "results": [
                {"table": "dws_x", "valid": True, "sql_type": "DDL"},
                {"table": "dws_x", "valid": True, "skipped": True, "sql_type": "DML_INSERT"},
            ],
        }

    monkeypatch.setattr(mod.SandboxSQL, "validate_design_plan", fake_plan)

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    state["design_plan"] = DWDesignPlan(task_id="t", modeling_goal="goal")
    failed = await mod.validate_node_sql(state, "design_dws")
    assert failed == 0
    assert any("1 DML skipped" in w for w in state["warnings"])


@pytest.mark.asyncio
async def test_invariant_failure_message_includes_details(monkeypatch):
    from wren_agent.graph import MAX_DESIGN_REVISIONS
    from wren_agent.models.task import TaskStatus, ValidationLevel, ValidationResult
    from wren_agent.nodes import validation
    from wren_agent.nodes.validation import harness_validate
    from wren_agent.models.design import DWSDesign, SQLArtifact

    async def fake_structure(_artifact):
        return ValidationResult(level=ValidationLevel.STRUCTURE, layer="DWS", passed=True)

    async def fake_semantic_batch(_artifacts, _state):
        return [ValidationResult(level=ValidationLevel.SEMANTIC, layer="DWS", passed=None)]

    monkeypatch.setattr(validation, "_validate_structure", fake_structure)
    monkeypatch.setattr(validation, "_generate_semantic_assertions_batch", fake_semantic_batch)
    monkeypatch.setattr(validation, "_rewrite_warehouse_plan_table_refs", lambda *a, **k: 0)
    monkeypatch.setattr(
        validation,
        "_validate_materialization_plan",
        lambda *_: [ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer="DWS",
            passed=False,
            failures=[{
                "check": "warehouse_source_table_explored",
                "detail": "dws_x references missing source: db_dws.t1",
            }],
        )],
    )

    state = create_initial_state("t", "goal", [], {}, source_mode="warehouse")
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="s", summary_table="dws_x", source_dwd_table="t1",
        source_tables=["db_dws.t1"], grain=["stat_date"],
    )]
    plan.sql_artifacts = [
        SQLArtifact(layer="DWS", target_table="dws_x", sql_type="DDL", sql="CREATE TABLE dws_x(x INT)"),
        SQLArtifact(layer="DWS", target_table="dws_x", sql_type="DML_INSERT", sql="INSERT INTO dws_x SELECT 1"),
    ]
    state["design_plan"] = plan
    state["design_revision_count"] = MAX_DESIGN_REVISIONS

    result = await harness_validate(state)
    assert result["status"] == TaskStatus.FAILED
    assert any("warehouse_source_table_explored" in e for e in result["errors"])
