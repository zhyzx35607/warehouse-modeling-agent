"""Tests for auto (unattended) task mode — auto-answered clarifications and auto-approvals."""

import asyncio

import pytest

from wren_agent.models.design import DWDesignPlan
from wren_agent.models.task import (
    ApprovalItem,
    ApprovalStatus,
    ClarificationItem,
    ClarificationStatus,
)
from wren_agent.state import create_initial_state


@pytest.fixture
def offline(monkeypatch):
    """Isolate nodes from PG — patch module-level engine persistence helpers.

    Nodes import these at call time (`from wren_agent.engine import ...`),
    so patching the source module attributes is sufficient.
    """
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("wren_agent.engine.persist_clarification_answer", _noop)
    monkeypatch.setattr("wren_agent.engine.persist_approval_response", _noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", _noop)


# ── _normalize_recommended ──────────────────────────────────────


class TestNormalizeRecommended:
    @pytest.mark.parametrize("raw,candidates,expected", [
        ("b", ["a", "b"], "b"),                    # verbatim hit
        (" B ", ["a", "B"], "B"),                  # whitespace/case tolerant
        ("b", ["a", "B"], "B"),                    # matched back to canonical text
        (1, ["a", "b"], "b"),                      # int index
        ("1", ["a", "b"], "b"),                    # digit-string index
        (9, ["a", "b"], "a"),                      # out-of-range index → first candidate
        (None, ["a", "b"], "a"),                   # missing → first candidate
        ("", ["a", "b"], "a"),                     # empty → first candidate
        (True, ["a", "b"], "a"),                   # bool is not an index
        ("free text", ["a", "b"], "free text"),    # unmatched text kept as-is
        (None, [], None),                          # nothing to recommend
        ("custom", [], "custom"),                  # free-text recommendation
    ])
    def test_normalize(self, raw, candidates, expected):
        from wren_agent.nodes.grill import _normalize_recommended
        assert _normalize_recommended(raw, candidates) == expected


# ── wait_human auto branch ──────────────────────────────────────


def _auto_state(**kwargs):
    return create_initial_state("auto-task-1", "goal", [], {}, mode="auto", **kwargs)


class TestWaitHumanAuto:
    @pytest.mark.asyncio
    async def test_auto_uses_recommended(self, offline):
        from wren_agent.nodes.grill import wait_human
        from wren_agent.graph import route_after_human_response

        state = _auto_state()
        c = ClarificationItem(
            question="时间字段含义?", blocking=True,
            candidates=["业务发生时间", "数据更新时间"],
            recommended="数据更新时间",
        )
        state["clarifications"] = [c]

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert c.status == ClarificationStatus.ANSWERED
        assert c.answer == "数据更新时间"
        assert c.answered_at is not None
        assert result["pending_human_input"] is False
        assert route_after_human_response(result) == "detect_schema_drifts"

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_first_candidate(self, offline):
        from wren_agent.nodes.grill import wait_human

        state = _auto_state()
        c = ClarificationItem(
            question="q?", blocking=True, candidates=["opt1", "opt2"],
        )
        state["clarifications"] = [c]

        await asyncio.wait_for(wait_human(state), timeout=5)

        assert c.status == ClarificationStatus.ANSWERED
        assert c.answer == "opt1"

    @pytest.mark.asyncio
    async def test_auto_skips_freetext_without_candidates(self, offline):
        from wren_agent.nodes.grill import wait_human
        from wren_agent.graph import route_after_human_response

        state = _auto_state()
        c = ClarificationItem(question="请列出枚举值及含义?", blocking=True)
        state["clarifications"] = [c]

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert c.status == ClarificationStatus.SKIPPED
        assert c.answer is None
        assert any("auto mode skipped" in w for w in result["warnings"])
        # SKIPPED is not pending → routing proceeds to drift detection
        assert route_after_human_response(result) == "detect_schema_drifts"

    @pytest.mark.asyncio
    async def test_auto_answers_nonblocking_too(self, offline):
        from wren_agent.nodes.grill import wait_human

        state = _auto_state()
        blocking = ClarificationItem(question="q1?", blocking=True, candidates=["a"])
        nonblocking = ClarificationItem(question="q2?", blocking=False, candidates=["b"])
        state["clarifications"] = [blocking, nonblocking]

        await asyncio.wait_for(wait_human(state), timeout=5)

        assert blocking.status == ClarificationStatus.ANSWERED
        assert nonblocking.status == ClarificationStatus.ANSWERED
        assert nonblocking.answer == "b"

    @pytest.mark.asyncio
    async def test_manual_mode_unaffected_when_no_pending(self, offline):
        """Manual mode with nothing pending still takes the skip guard."""
        from wren_agent.nodes.grill import wait_human

        state = create_initial_state("manual-task", "goal", [], {})
        result = await asyncio.wait_for(wait_human(state), timeout=5)
        assert result["pending_human_input"] is False

    @pytest.mark.asyncio
    async def test_manual_mode_does_not_pause_for_nonblocking_items(
        self, offline, monkeypatch
    ):
        from wren_agent.nodes.grill import wait_human

        state = create_initial_state("manual-nonblocking", "goal", [], {})
        state["clarifications"] = [
            ClarificationItem(question="optional?", blocking=False)
        ]

        async def must_not_pause(*_args, **_kwargs):
            pytest.fail("a non-blocking clarification paused the pipeline")

        monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", must_not_pause)

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert result["pending_human_input"] is False
        assert result["status"] == "designing"


class TestWaitHumanManualSync:
    @pytest.mark.asyncio
    async def test_persisted_answer_is_synced_before_pause(self, offline, monkeypatch):
        """Answers saved between wait cycles must prevent another pause."""
        from wren_agent.nodes.grill import wait_human

        state = create_initial_state("manual-sync", "goal", [], {})
        item = ClarificationItem(question="q?", blocking=True)
        state["clarifications"] = [item]

        async def sync_answer(_state):
            item.status = ClarificationStatus.ANSWERED
            item.answer = "done"

        async def must_not_pause(*_args, **_kwargs):
            pytest.fail("wait_human paused despite a persisted answer")

        monkeypatch.setattr(
            "wren_agent.nodes.grill._sync_clarification_answers_from_pg",
            sync_answer,
        )
        monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", must_not_pause)

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert result["pending_human_input"] is False
        assert item.status == ClarificationStatus.ANSWERED

    @pytest.mark.asyncio
    async def test_answer_during_pause_registration_skips_wait(
        self, offline, monkeypatch
    ):
        """Close the sync-to-pause race for rapidly submitted answer batches."""
        from wren_agent.nodes.grill import wait_human

        state = create_initial_state("manual-race", "goal", [], {})
        item = ClarificationItem(question="q?", blocking=True)
        state["clarifications"] = [item]
        sync_calls = 0

        async def sync_on_second_call(_state):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                item.status = ClarificationStatus.ANSWERED
                item.answer = "done"

        async def noop(*_args, **_kwargs):
            return None

        def sync_noop(*_args, **_kwargs):
            return None

        async def must_not_wait(*_args, **_kwargs):
            pytest.fail("wait_human waited after the second sync resolved all items")

        monkeypatch.setattr(
            "wren_agent.nodes.grill._sync_clarification_answers_from_pg",
            sync_on_second_call,
        )
        monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", noop)
        monkeypatch.setattr("wren_agent.tools.pg_store.clear_task_pause", noop)
        monkeypatch.setattr("wren_agent.engine.wait_for_resume", must_not_wait)
        monkeypatch.setattr("wren_agent.tools.event_emitter.emit_interrupt", sync_noop)
        monkeypatch.setattr("wren_agent.tools.event_emitter.emit_status", sync_noop)

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert sync_calls == 3  # initial, post-registration, final all-answer sync
        assert result["pending_human_input"] is False
        assert result["status"] == "designing"


    @pytest.mark.asyncio
    async def test_ods_migration_skips_blocking_grill(self, offline, monkeypatch):
        """1:1 whole-DB copy must not park on date/enum semantics cards."""
        from wren_agent.graph import route_after_human_response
        from wren_agent.nodes.grill import wait_human

        state = create_initial_state(
            "ods-task",
            "将所选业务源库中的全部业务表一对一迁移到 Doris ODS。",
            [],
            {},
        )
        state["business_task_type"] = "full"
        item = ClarificationItem(
            question="'t.dateA' 代表什么时间？",
            blocking=True,
            candidates=["业务发生时间", "数据更新时间"],
        )
        state["clarifications"] = [item]

        async def noop_sync(_state):
            return None

        monkeypatch.setattr(
            "wren_agent.nodes.grill._sync_clarification_answers_from_pg",
            noop_sync,
        )

        result = await asyncio.wait_for(wait_human(state), timeout=5)

        assert item.status == ClarificationStatus.ANSWERED
        assert result["pending_human_input"] is False
        assert route_after_human_response(result) == "detect_schema_drifts"


class TestHumanApprovalAuto:
    @pytest.mark.asyncio
    async def test_auto_approves_all(self, offline):
        from wren_agent.nodes.approval import human_approval, process_approval
        from wren_agent.graph import route_after_approval

        state = _auto_state()
        state["design_plan"] = DWDesignPlan(
            task_id=state["task_id"], modeling_goal="goal",
        )

        # Must not hang on the 30-minute pause
        result = await asyncio.wait_for(human_approval(state), timeout=5)

        items = result["approval_items"]
        assert len(items) == 1
        assert items[0].status == ApprovalStatus.APPROVED
        assert "[auto]" in (items[0].comment or "")
        assert items[0].resolved_at is not None
        assert result["pending_human_input"] is False

        result = await process_approval(result)
        assert result["design_plan"].approved is True
        assert route_after_approval(result) == "dag_deploy"

    @pytest.mark.asyncio
    async def test_auto_refuses_logic_execution_approval(self, offline):
        """Auto-approving logic execution errors would redeploy the same SQL."""
        from wren_agent.graph import route_after_approval
        from wren_agent.models.task import ErrorClassification, ErrorType, TaskStatus
        from wren_agent.nodes.approval import human_approval

        state = _auto_state()
        item = ApprovalItem(
            title="DAG 执行错误", description="d",
            artifact_type="execution", artifact_id="ti-1",
        )
        state["approval_items"] = [item]
        state["approval_context"] = "execution"
        state["error_classifications"] = [
            ErrorClassification(
                task_instance_id="ti-1",
                error_type=ErrorType.LOGIC,
                raw_log="Unknown column 'x'",
                summary="SQL references a column that does not exist (agent-fixable)",
                action="escalate",
            )
        ]

        result = await asyncio.wait_for(human_approval(state), timeout=5)

        assert result["status"] == TaskStatus.FAILED
        assert any("refusing to auto-approve unrepaired SQL" in e for e in result["errors"])
        assert item.status == ApprovalStatus.REJECTED
        assert route_after_approval(result) == "generate_report"


    @pytest.mark.asyncio
    async def test_auto_increments_rounds_for_execution_items(self, offline):
        from wren_agent.nodes.approval import human_approval

        state = _auto_state()
        state["approval_items"] = [
            ApprovalItem(title="DAG 执行错误", description="d", artifact_type="execution")
        ]

        result = await asyncio.wait_for(human_approval(state), timeout=5)

        assert result["auto_approval_rounds"] == 1
        assert result["approval_items"][0].status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_auto_execution_guard_fails_fast(self, offline):
        """After 2 auto-approved execution rounds, auto mode fails the task
        instead of pausing — unattended, a manual pause only times out after
        30 minutes and resumes the same loop."""
        from wren_agent.graph import route_after_approval
        from wren_agent.models.task import TaskStatus
        from wren_agent.nodes.approval import human_approval

        state = _auto_state()
        state["auto_approval_rounds"] = 2
        item = ApprovalItem(title="DAG 执行错误", description="d", artifact_type="execution")
        state["approval_items"] = [item]

        # Must not hang on the 30-minute pause
        result = await asyncio.wait_for(human_approval(state), timeout=5)

        assert result["status"] == TaskStatus.FAILED
        assert any("execution errors persisted" in e for e in result["errors"])
        assert item.status == ApprovalStatus.REJECTED
        # Guard round counter untouched
        assert result["auto_approval_rounds"] == 2
        # Routing funnels to the failure report instead of redeploying
        assert route_after_approval(result) == "generate_report"


# ── Routing is mode-agnostic (regression) ───────────────────────


class TestRoutingModeAgnostic:
    def test_routes_unchanged_under_auto_mode(self):
        """Auto mode is consumed inside nodes — route functions must not care."""
        from wren_agent.graph import (
            route_after_grill,
            route_after_human_response,
            route_after_approval,
            route_after_validation,
        )

        state = _auto_state()
        assert route_after_grill(state) == "detect_schema_drifts"
        assert route_after_human_response(state) == "detect_schema_drifts"
        assert route_after_approval(state) == "dag_deploy"
        state["design_plan"] = DWDesignPlan(
            task_id=state["task_id"], modeling_goal="goal"
        )
        state["all_validations_passed"] = True
        assert route_after_validation(state) == "human_approval"
