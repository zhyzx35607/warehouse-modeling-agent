"""Regression tests for crash recovery classification and loop guards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wren_agent.api.routes import _failure_kind
from wren_agent.engine import AgentRunner, MAX_RECOVERY_ATTEMPTS


def _checkpoint(**state):
    cp = {"channel_values": state}
    cp["next"] = state.pop("next", [])
    return SimpleNamespace(checkpoint=cp)


def test_failure_kind_distinguishes_validation_from_crash():
    assert _failure_kind(
        {"errors": ["Critical design invariants failed; task cannot enter approval"]},
        "failed",
    ) == "validation"
    assert _failure_kind({"errors": ["connection reset"]}, "failed") == "crash"
    assert _failure_kind({"errors": []}, "running") == ""


@pytest.mark.asyncio
async def test_validation_failure_cannot_enter_crash_recovery():
    runner = AgentRunner()
    runner.get_task_state = AsyncMock(return_value={"status": "failed"})
    runner._checkpointer = AsyncMock()
    runner._checkpointer.aget_tuple.return_value = _checkpoint(
        validation_blocking=True,
        failure_kind="validation",
        recovery_count=0,
    )
    runner._update_task_pg = AsyncMock()

    result = await runner.recover_task("task-validation")

    assert result["status"] == "validation_failed"
    runner._update_task_pg.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_attempts_are_capped():
    runner = AgentRunner()
    runner.get_task_state = AsyncMock(return_value={"status": "failed"})
    runner._checkpointer = AsyncMock()
    runner._checkpointer.aget_tuple.return_value = _checkpoint(
        recovery_count=MAX_RECOVERY_ATTEMPTS,
        errors=["worker process stopped unexpectedly"],
    )
    runner._update_task_pg = AsyncMock()

    result = await runner.recover_task("task-crash-loop")

    assert result["status"] == "recovery_limit_reached"
    runner._update_task_pg.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_checkpoint_restart_resets_stale_failure_state():
    """Regression a1af4bed: recovering a task whose graph FINISHED (terminal
    checkpoint) restarts from __start__ — the stale failure channels
    (error_classifications, approval_items, ...) must be cleared, or the fresh
    run re-opens the previous execution-error approval and auto mode fails it
    again at human_approval."""
    runner = AgentRunner()
    runner.get_task_state = AsyncMock(return_value={"status": "failed"})
    runner._checkpointer = AsyncMock()
    runner._checkpointer.aget_tuple.return_value = _checkpoint(
        next=[],
        recovery_count=1,
        errors=["old error"],
        error_classifications=[{"task_instance_id": "1415", "action": "escalate"}],
        approval_items=[{"title": "DAG 执行错误: 1415"}],
        execution_results=[{"logs": ["stale"]}],
        auto_approval_rounds=1,
    )
    runner._update_task_pg = AsyncMock()
    runner._run_recovery = AsyncMock()

    result = await runner.recover_task("task-terminal")

    assert result["status"] == "recovering"
    runner._run_recovery.assert_called_once()
    _, config, count, reset = runner._run_recovery.call_args.args
    assert reset["errors"] == []
    assert reset["error_classifications"] == []
    assert reset["approval_items"] == []
    assert reset["execution_results"] == []
    assert reset["auto_approval_rounds"] == 0
    assert reset["failure_kind"] == ""
    assert reset["ds_project_code"] is None


@pytest.mark.asyncio
async def test_mid_graph_recovery_keeps_failure_context():
    """A NON-terminal checkpoint (true mid-graph crash) resumes the pending
    node — failure channels must NOT be reset, error_handling needs them."""
    runner = AgentRunner()
    runner.get_task_state = AsyncMock(return_value={"status": "failed"})
    runner._checkpointer = AsyncMock()
    runner._checkpointer.aget_tuple.return_value = _checkpoint(
        next=["design_ads"],
        recovery_count=0,
        errors=["connection reset"],
    )
    runner._update_task_pg = AsyncMock()
    runner._run_recovery = AsyncMock()

    result = await runner.recover_task("task-midgraph")

    assert result["status"] == "recovering"
    runner._run_recovery.assert_called_once()
    _, config, count, reset = runner._run_recovery.call_args.args
    assert reset == {}
