"""Regression tests for the task-a0a63385 infinite loop.

Root causes:
1. final_validate judged "0 rows written" from execution_results.rows_written,
   which execute_tasks hardcodes to 0 — every successful run failed validation.
2. error_handling could repeatedly enter human_approval; auto mode
   auto-approves, and the approval-side loop guard only counts rounds when an
   execution approval item exists (validation failures produce none). The
   error guard now marks the task failed after three rounds and the graph ends
   immediately instead of opening another approval.
"""

import pytest

from wren_agent.models.design import ADSDesign, DWDesignPlan, DWSDesign
from wren_agent.models.task import ExecutionResult, TaskStatus


def _state(**overrides):
    base = dict(
        task_id="a0a63385-af77-44ee-a48d-a3b848ac5297",
        errors=[],
        warnings=[],
        error_classifications=[],
        execution_results=[ExecutionResult(task_instance_id="1", success=True)],
        design_plan=DWDesignPlan(
            task_id="a0a63385-af77-44ee-a48d-a3b848ac5297",
            modeling_goal="test",
            dws_designs=[DWSDesign(
                summary_name="s", summary_table="dws_x", source_dwd_table="dwd_x",
            )],
            ads_designs=[ADSDesign(application_name="a", ads_table="ads_x")],
        ),
    )
    base.update(overrides)
    return base


class _FakeDoris:
    def __init__(self, counts):
        self._counts = counts

    async def get_row_count(self, table):
        return self._counts.get(table, 0)


def _patch_doris(monkeypatch, counts=None, raise_on_init=False):
    if raise_on_init:
        def _boom():
            raise RuntimeError("doris unreachable")
        monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", _boom)
    else:
        monkeypatch.setattr(
            "wren_agent.tools.doris_client.DorisClient",
            lambda: _FakeDoris(counts or {}),
        )


# ── error_handling loop guard ─────────────────────────────────


class TestErrorHandlingLoopGuard:
    @pytest.mark.asyncio
    async def test_first_visit_counts_round(self):
        from wren_agent.nodes.deployment import error_handling

        state = await error_handling(_state())
        assert state["error_recovery_rounds"] == 1
        assert state.get("status") != TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_third_visit_fails_task(self):
        from wren_agent.nodes.deployment import error_handling

        state = _state(error_recovery_rounds=2)
        state = await error_handling(state)
        assert state["error_recovery_rounds"] == 3
        assert state["status"] == TaskStatus.FAILED
        assert any("exhausted" in e for e in state["errors"])


# ── final_validate zero-row guard (real Doris counts) ─────────


class TestFinalValidateZeroRows:
    @pytest.mark.asyncio
    async def test_all_targets_empty_fails(self, monkeypatch):
        _patch_doris(monkeypatch, {"dws_x": 0, "ads_x": 0})
        from wren_agent.nodes.validation import final_validate

        state = await final_validate(_state())
        assert state["all_validations_passed"] is False
        assert any("没有数据" in e for e in state["errors"])

    @pytest.mark.asyncio
    async def test_any_empty_target_fails(self, monkeypatch):
        _patch_doris(monkeypatch, {"dws_x": 0, "ads_x": 12})
        from wren_agent.nodes.validation import final_validate

        state = await final_validate(_state())
        assert state["all_validations_passed"] is False
        assert any("dws_x" in e for e in state["errors"])

    @pytest.mark.asyncio
    async def test_doris_unreachable_blocks_success(self, monkeypatch):
        # Without direct Doris verification the report cannot truthfully claim
        # that materialization succeeded.
        _patch_doris(monkeypatch, raise_on_init=True)
        from wren_agent.nodes.validation import final_validate

        state = await final_validate(_state())
        assert state["all_validations_passed"] is False
        assert any("Unable to verify" in e for e in state["errors"])


@pytest.mark.asyncio
async def test_approve_unrepaired_logic_execution_does_not_redeploy():
    from wren_agent.graph import route_after_approval
    from wren_agent.models.task import (
        ApprovalItem, ApprovalStatus, ErrorClassification, ErrorType, TaskStatus,
    )
    from wren_agent.nodes.approval import process_approval
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-exec", "goal", [], {})
    state["approval_context"] = "execution"
    state["approval_items"] = [
        ApprovalItem(
            title="DAG 执行错误",
            description="d",
            artifact_type="execution",
            artifact_id="ti-1",
            status=ApprovalStatus.APPROVED,
        )
    ]
    state["error_classifications"] = [
        ErrorClassification(
            task_instance_id="ti-1",
            error_type=ErrorType.LOGIC,
            raw_log="Unknown column 'x'",
            summary="bad column",
            action="escalate",
        )
    ]

    result = await process_approval(state)
    assert result["status"] == TaskStatus.FAILED
    assert route_after_approval(result) == "generate_report"


@pytest.mark.asyncio
async def test_human_approval_skips_logic_execution_items():
    """Logic/data escalate errors must not open a new execution approval."""
    from wren_agent.models.task import ErrorClassification, ErrorType
    from wren_agent.nodes.approval import human_approval
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-exec", "goal", [], {})
    state["error_classifications"] = [
        ErrorClassification(
            task_instance_id="ti-1",
            error_type=ErrorType.LOGIC,
            raw_log="Unknown column 'group_id'",
            summary="SQL references a column that does not exist (agent-fixable)",
            action="escalate",
        )
    ]

    result = await human_approval(state)
    assert result["status"].value == "failed" or result.get("status")
    from wren_agent.models.task import TaskStatus
    assert result["status"] == TaskStatus.FAILED
    assert not any(a.artifact_type == "execution" for a in result.get("approval_items", []))
    assert any("without any approvable items" in e for e in result.get("errors", []))


def _stale_approved_plan_state(**overrides):
    """State after design was approved, deployed, executed, then final_validate
    failed — the situation that re-opens human_approval with nothing pending
    (task 4cfb5949)."""
    from wren_agent.models.task import ApprovalItem, ApprovalStatus
    from wren_agent.state import create_initial_state

    state = create_initial_state("4cfb5949-1846-48e8-8084-9b0b0998375f", "goal", [], {})
    plan = DWDesignPlan(
        task_id=state["task_id"],
        modeling_goal="goal",
        dws_designs=[DWSDesign(
            summary_name="s", summary_table="dws_stock_inventory_monthly",
            source_dwd_table="dwd_x",
        )],
    )
    plan.approved = True
    state["design_plan"] = plan
    state["approval_items"] = [
        ApprovalItem(
            title="数仓建设方案审批",
            description="already decided",
            artifact_type="design_plan",
            artifact_id=plan.id,
            status=ApprovalStatus.APPROVED,
        )
    ]
    state["approval_items"][0]._persisted = True
    state.update(overrides)
    return state


def _patch_hitl_pause(monkeypatch, paused, payload=None):
    async def noop(*_args, **_kwargs):
        return None

    async def resume(*_args, **_kwargs):
        paused.append(True)
        return payload if payload is not None else {}

    monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", noop)
    monkeypatch.setattr("wren_agent.tools.pg_store.clear_task_pause", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)
    monkeypatch.setattr("wren_agent.engine.wait_for_resume", resume)


@pytest.mark.asyncio
async def test_stale_approved_plan_without_new_work_fails_instead_of_pausing(
    monkeypatch,
):
    """Re-entering human_approval with only an already-approved design_plan
    and no post-execution failure must not pause — the dashboard would show
    cards with no Approve/Reject buttons (task 4cfb5949 without the empty
    materialization card)."""
    from wren_agent.models.task import TaskStatus
    from wren_agent.nodes.approval import human_approval

    async def must_not_pause(*_args, **_kwargs):
        pytest.fail("stale approved items entered a human wait")

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", must_not_pause)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)

    result = await human_approval(_stale_approved_plan_state())
    assert result["status"] == TaskStatus.FAILED
    assert result["pending_human_input"] is False
    pending = [a for a in result["approval_items"] if a.status.value == "pending"]
    assert pending == []


@pytest.mark.asyncio
async def test_empty_materialization_opens_pending_execution_card(monkeypatch):
    """task 4cfb5949: DAG succeeded, Doris target had 0 rows, error_handling
    routed back to human_approval. The approved design_plan card is not
    actionable — open a new pending card so the human can retry deploy or
    send the design back."""
    from wren_agent.models.task import ApprovalStatus
    from wren_agent.nodes.approval import human_approval

    paused = []
    _patch_hitl_pause(monkeypatch, paused)

    state = _stale_approved_plan_state(
        all_validations_passed=False,
        execution_results=[ExecutionResult(task_instance_id="10601", success=True)],
        errors=["执行完成，但物化表没有数据: "
                "dws_gz_test.dws_stock_inventory_monthly"],
    )
    result = await human_approval(state)
    pending = [
        a for a in result["approval_items"]
        if a.status == ApprovalStatus.PENDING
    ]
    assert len(pending) == 1, f"expected one pending card, got {result['approval_items']}"
    assert pending[0].artifact_type == "execution"
    assert "没有数据" in pending[0].description
    assert "dws_stock_inventory_monthly" in pending[0].description
    assert result["approval_context"] == "execution"
    assert paused, "manual mode must pause so the new card can be acted on"


@pytest.mark.asyncio
async def test_reject_empty_materialization_goes_to_design_revise():
    from wren_agent.graph import route_after_approval
    from wren_agent.models.task import ApprovalItem, ApprovalStatus
    from wren_agent.nodes.approval import process_approval
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-empty-reject", "goal", [], {})
    state["approval_context"] = "execution"
    state["approval_items"] = [
        ApprovalItem(
            title="执行后校验未通过",
            description="empty",
            artifact_type="execution",
            artifact_id="final_validate",
            status=ApprovalStatus.REJECTED,
        )
    ]
    result = await process_approval(state)
    assert route_after_approval(result) == "design_revise"


@pytest.mark.asyncio
async def test_approve_empty_materialization_retries_deploy():
    from wren_agent.graph import route_after_approval
    from wren_agent.models.task import ApprovalItem, ApprovalStatus
    from wren_agent.nodes.approval import process_approval
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-empty-approve", "goal", [], {})
    state["approval_context"] = "execution"
    state["approval_items"] = [
        ApprovalItem(
            title="执行后校验未通过",
            description="empty",
            artifact_type="execution",
            artifact_id="final_validate",
            status=ApprovalStatus.APPROVED,
        )
    ]
    result = await process_approval(state)
    assert route_after_approval(result) == "dag_deploy"
