"""Regression 5385ea71: first ODS incremental run must bootstrap a full load.

DataX SQL is `WHERE update_time > '${last_run_time}'` and the workflow global
param expands to yesterday. The agent's first execute_tasks start then reads
zero source rows even when the table has history, so final_validate reports
empty materialized ODS targets.
"""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDesignPlan, ODSDesign
from wren_agent.state import create_initial_state


def _ods_plan(*, incremental_column: str | None = "update_time") -> DWDesignPlan:
    plan = DWDesignPlan(task_id="t", modeling_goal="ods")
    plan.ods_designs = [ODSDesign(
        source_table="cwzj_financial_funds",
        source_db_id="src",
        ods_table="ods_cwzj_financial_funds",
        incremental_strategy="incremental_with_full_reconcile",
        incremental_column=incremental_column,
    )]
    return plan


class _FakeDS:
    def __init__(self):
        self.start_calls: list[dict] = []

    async def resolve_workflow_code(self, *_args, **_kwargs):
        return 1

    async def start_workflow(self, project_code, workflow_code, start_params=None):
        self.start_calls.append({
            "project_code": project_code,
            "workflow_code": workflow_code,
            "start_params": start_params,
        })
        return 99

    async def get_instance_tasks(self, *_args, **_kwargs):
        return []

    def close(self):
        pass


@pytest.mark.asyncio
async def test_execute_tasks_bootstraps_incremental_ods_last_run_time(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _FakeDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)

    async def _poll(*_args, **_kwargs):
        return True, [], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    state = create_initial_state("t", "goal", [], {})
    state["ds_project_code"] = 123
    state["ds_dags"] = ["ods_cwzj_financial_funds_sync"]
    state["design_plan"] = _ods_plan()

    await dep.execute_tasks(state)

    assert fake.start_calls[0]["start_params"] == {
        "last_run_time": "1970-01-01 00:00:00",
    }


@pytest.mark.asyncio
async def test_execute_tasks_does_not_bootstrap_full_ods_sync(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _FakeDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)

    async def _poll(*_args, **_kwargs):
        return True, [], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    state = create_initial_state("t", "goal", [], {})
    state["ds_project_code"] = 123
    state["ds_dags"] = ["ods_cwzj_financial_funds_sync"]
    state["design_plan"] = _ods_plan(incremental_column=None)

    await dep.execute_tasks(state)

    assert fake.start_calls[0]["start_params"] in (None, {})


def test_ods_metadata_ddl_has_defaults_so_datax_can_omit_columns():
    """Stream Load maps business columns only; _ods_* must have DEFAULT
    or Doris rejects the row as NULL on NOT NULL (task 755c5c60)."""
    from wren_agent.nodes.design_layers import (
        _ensure_ods_metadata_defaults,
        _format_ods_column_ddl,
        _ods_metadata_fields,
    )

    ddl = ",\n  ".join(_format_ods_column_ddl(f) for f in _ods_metadata_fields())
    assert "_ods_load_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP" in ddl
    assert "_ods_source_db VARCHAR(64) NOT NULL DEFAULT ''" in ddl
    assert "_ods_sync_type VARCHAR(32) NOT NULL DEFAULT ''" in ddl

    llm_ddl = (
        "CREATE TABLE IF NOT EXISTS ods_orders (\n"
        "  id INT NOT NULL COMMENT '主键',\n"
        "  _ods_load_time DATETIME NOT NULL COMMENT '数据加载时间',\n"
        "  _ods_source_db VARCHAR(64) NOT NULL COMMENT '来源数据库标识',\n"
        "  _ods_sync_type VARCHAR(32) NOT NULL COMMENT '同步方式(全量/增量)'\n"
        ")"
    )
    fixed = _ensure_ods_metadata_defaults(llm_ddl)
    assert "DEFAULT CURRENT_TIMESTAMP" in fixed
    assert "_ods_source_db VARCHAR(64) NOT NULL DEFAULT ''" in fixed
    assert "_ods_sync_type VARCHAR(32) NOT NULL DEFAULT ''" in fixed


@pytest.mark.asyncio
async def test_ods_migration_designs_every_explored_table(monkeypatch):
    """Whole-DB migration must not let the LLM pick a 5-table subset."""
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.nodes.design_layers import design_ods

    async def boom(*_args, **_kwargs):
        raise AssertionError("ods_migration must skip LLM generation")

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "wren_agent.nodes.design_layers._llm_generate_layer", boom,
    )
    monkeypatch.setattr(
        "wren_agent.nodes.design_layers._sandbox_validate", noop,
    )
    monkeypatch.setattr(
        "wren_agent.nodes.design_layers._llm_review_layer", noop,
    )

    state = create_initial_state("t", "整库迁移", [], {})
    state["business_task_type"] = "ods_migration"
    names = [f"financial_table_{i}" for i in range(12)]
    state["explorations"] = {
        "src": SchemaExploration(
            source_db_id="src",
            tables=[
                TableSchema(
                    name=n,
                    db_type="mysql",
                    fields=[FieldSchema(name="id", data_type="INT", nullable=False)],
                )
                for n in names
            ],
        ),
    }

    result = await design_ods(state)
    designed = [d.source_table for d in result["design_plan"].ods_designs]
    assert designed == names
    ddl = result["design_plan"].sql_artifacts[0].sql
    assert "DEFAULT CURRENT_TIMESTAMP" in ddl
