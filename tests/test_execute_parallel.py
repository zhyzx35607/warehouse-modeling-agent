"""execute_tasks must run ODS `_sync` DAGs concurrently, not start→poll→next.

Whole-DB migration (task 70a5613d) spent ~2.3h on 410 serial DataX runs
because each table waited for poll+logs before the next start. The docstring
already said "ODS (parallel)"; this locks that behavior in.
"""

from __future__ import annotations

import asyncio

import pytest

from wren_agent.state import create_initial_state


class _CountingDS:
    def __init__(self):
        self.start_calls: list[str] = []
        self._codes: dict[str, int] = {}
        self._id_to_name: dict[int, str] = {}
        self._next_instance = 1
        self.close_count = 0

    async def resolve_workflow_code(self, _project_code, dag_name):
        if dag_name not in self._codes:
            self._codes[dag_name] = len(self._codes) + 1
        return self._codes[dag_name]

    async def start_workflow(self, _project_code, workflow_code, start_params=None):
        name = next(n for n, c in self._codes.items() if c == workflow_code)
        self.start_calls.append(name)
        instance_id = self._next_instance
        self._next_instance += 1
        self._id_to_name[instance_id] = name
        return instance_id

    async def get_instance_tasks(self, *_args, **_kwargs):
        return []

    def close(self):
        self.close_count += 1


def _state_with_dags(dags: list[str]):
    state = create_initial_state("t", "整库迁移", [], {})
    state["ds_project_code"] = 123
    state["ds_dags"] = dags
    return state


@pytest.mark.asyncio
async def test_ods_sync_dags_poll_concurrently(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _CountingDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)
    monkeypatch.setattr(dep, "_ods_execute_concurrency", lambda: 8)

    inflight = 0
    max_inflight = 0

    async def _poll(_ds, _project_code, instance_id, **_kwargs):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return True, [fake._id_to_name[instance_id]], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    dags = [f"ods_table_{i}_sync" for i in range(5)]
    result = await dep.execute_tasks(_state_with_dags(dags))

    assert max_inflight > 1
    assert [r.task_instance_id for r in result["execution_results"]] == ["1", "2", "3", "4", "5"]
    assert all(r.success for r in result["execution_results"])


@pytest.mark.asyncio
async def test_ods_execute_concurrency_is_capped(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _CountingDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)
    monkeypatch.setattr(dep, "_ods_execute_concurrency", lambda: 2)

    inflight = 0
    max_inflight = 0

    async def _poll(_ds, _project_code, instance_id, **_kwargs):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.04)
        inflight -= 1
        return True, [], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    dags = [f"ods_table_{i}_sync" for i in range(6)]
    await dep.execute_tasks(_state_with_dags(dags))

    assert 1 < max_inflight <= 2


@pytest.mark.asyncio
async def test_dw_transform_waits_until_all_ods_finish(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _CountingDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)
    monkeypatch.setattr(dep, "_ods_execute_concurrency", lambda: 8)

    ods_polls_done: list[str] = []
    dw_started_after_ods: list[bool] = []

    async def _poll(_ds, _project_code, instance_id, **_kwargs):
        name = fake._id_to_name[instance_id]
        if name.endswith("_sync"):
            await asyncio.sleep(0.03)
            ods_polls_done.append(name)
        return True, [], {}

    orig_start = fake.start_workflow

    async def _start(project_code, workflow_code, start_params=None):
        name = next(n for n, c in fake._codes.items() if c == workflow_code)
        if name.startswith("dw_transform_"):
            dw_started_after_ods.append(
                set(ods_polls_done) == {"ods_a_sync", "ods_b_sync"}
            )
        return await orig_start(project_code, workflow_code, start_params)

    fake.start_workflow = _start
    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    result = await dep.execute_tasks(_state_with_dags([
        "ods_a_sync", "ods_b_sync", "dw_transform_deadbeef",
    ]))

    assert dw_started_after_ods == [True]
    names = [next(iter(r.rows_written)) for r in result["execution_results"]]
    assert names == ["ods_a_sync", "ods_b_sync", "dw_transform_deadbeef"]


@pytest.mark.asyncio
async def test_ods_failure_does_not_block_sibling_tables(monkeypatch):
    from wren_agent.nodes import deployment as dep

    fake = _CountingDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)
    monkeypatch.setattr(dep, "_ods_execute_concurrency", lambda: 8)

    async def _poll(_ds, _project_code, instance_id, **_kwargs):
        name = fake._id_to_name[instance_id]
        return name != "ods_bad_sync", [], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    result = await dep.execute_tasks(_state_with_dags([
        "ods_ok_sync", "ods_bad_sync", "ods_other_sync",
    ]))
    outcomes = {next(iter(r.rows_written)): r.success for r in result["execution_results"]}
    assert outcomes == {
        "ods_ok_sync": True,
        "ods_bad_sync": False,
        "ods_other_sync": True,
    }


def test_unsuccessful_or_pending_dags_keeps_failures_and_unrun():
    from wren_agent.models.task import ExecutionResult
    from wren_agent.nodes.deployment import _unsuccessful_or_pending_dags

    results = [
        ExecutionResult(task_instance_id="1", success=True, rows_written={"ods_ok_sync": 0}),
        ExecutionResult(task_instance_id="2", success=False, rows_written={"ods_bad_sync": 0}),
    ]
    pending = _unsuccessful_or_pending_dags(
        ["ods_ok_sync", "ods_bad_sync", "ods_new_sync"],
        results,
    )
    assert pending == ["ods_bad_sync", "ods_new_sync"]


@pytest.mark.asyncio
async def test_execute_retries_only_failed_dags_and_keeps_successes(monkeypatch):
    """After a partial execute, do not start DataX again for tables that already succeeded
    (task 9af6aa77 re-deployed all 410 after 7 failures)."""
    from wren_agent.models.task import ExecutionResult
    from wren_agent.nodes import deployment as dep

    fake = _CountingDS()
    monkeypatch.setattr("wren_agent.tools.ds_api.get_ds_api", lambda: fake)
    monkeypatch.setattr(dep, "_ods_execute_concurrency", lambda: 8)

    async def _poll(*_args, **_kwargs):
        return True, [], {}

    monkeypatch.setattr(dep, "_poll_workflow_completion", _poll)

    state = _state_with_dags(["ods_ok_sync", "ods_bad_sync", "ods_other_sync"])
    state["execution_results"] = [
        ExecutionResult(task_instance_id="10", success=True, rows_written={"ods_ok_sync": 0}),
        ExecutionResult(task_instance_id="11", success=False, rows_written={"ods_bad_sync": 0}),
        ExecutionResult(task_instance_id="12", success=True, rows_written={"ods_other_sync": 0}),
    ]

    result = await dep.execute_tasks(state)
    assert fake.start_calls == ["ods_bad_sync"]
    outcomes = {next(iter(r.rows_written)): r.success for r in result["execution_results"]}
    assert outcomes == {
        "ods_ok_sync": True,
        "ods_bad_sync": True,
        "ods_other_sync": True,
    }
    assert [next(iter(r.rows_written)) for r in result["execution_results"]] == [
        "ods_ok_sync", "ods_bad_sync", "ods_other_sync",
    ]


def test_ods_sync_targets_for_deploy_skips_successful_tables():
    from wren_agent.models.design import DWDesignPlan, ODSDesign
    from wren_agent.models.task import ExecutionResult
    from wren_agent.nodes.deployment import _ods_sync_targets_for_deploy
    from wren_agent.state import create_initial_state

    plan = DWDesignPlan(task_id="t", modeling_goal="ods")
    plan.ods_designs = [
        ODSDesign(source_table="a", source_db_id="s", ods_table="ods_ok"),
        ODSDesign(source_table="b", source_db_id="s", ods_table="ods_bad"),
        ODSDesign(source_table="c", source_db_id="s", ods_table="ods_other"),
    ]
    state = create_initial_state("t", "整库迁移", [], {})
    state["ds_dags"] = ["ods_ok_sync", "ods_bad_sync", "ods_other_sync"]
    state["execution_results"] = [
        ExecutionResult(task_instance_id="1", success=True, rows_written={"ods_ok_sync": 0}),
        ExecutionResult(task_instance_id="2", success=False, rows_written={"ods_bad_sync": 0}),
        ExecutionResult(task_instance_id="3", success=True, rows_written={"ods_other_sync": 0}),
    ]
    names, designs = _ods_sync_targets_for_deploy(state, plan)
    assert names == ["ods_ok_sync", "ods_bad_sync", "ods_other_sync"]
    assert [d.ods_table for d in designs] == ["ods_bad"]
