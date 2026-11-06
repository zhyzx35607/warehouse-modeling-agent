"""One-click bulk approval: settle every pending item in a single resume."""

import pytest

from wren_agent.models.task import ApprovalItem, ApprovalStatus, SchemaDriftResult
from wren_agent.models.task import DriftType
from wren_agent.state import create_initial_state


def _patch_pause(monkeypatch, payload):
    async def noop(*_args, **_kwargs):
        return None

    async def resume(*_args, **_kwargs):
        return payload

    monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", noop)
    monkeypatch.setattr("wren_agent.tools.pg_store.clear_task_pause", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)
    monkeypatch.setattr("wren_agent.engine.wait_for_resume", resume)
    monkeypatch.setattr(
        "wren_agent.nodes.approval._sync_approval_responses_from_pg", noop,
    )
    monkeypatch.setattr(
        "wren_agent.tools.event_emitter.emit_interrupt", lambda *_a, **_k: None,
    )


def _drift_state(*table_names: str):
    state = create_initial_state("bulk-task", "goal", [], {}, mode="manual")
    state["approval_context"] = "schema_drift"
    state["schema_drifts"] = [
        SchemaDriftResult(
            table_name=name,
            drift_type=DriftType.TABLE_REMOVED,
            details=f"Table '{name}' removed",
            is_breaking=True,
        )
        for name in table_names
    ]
    state["approval_items"] = [
        ApprovalItem(
            title=f"Schema 破坏性变更确认: {name}",
            description="pending",
            artifact_type="drift_fix",
            artifact_id=name,
            status=ApprovalStatus.PENDING,
        )
        for name in table_names
    ]
    for item in state["approval_items"]:
        item._persisted = True
    return state


@pytest.mark.asyncio
async def test_bulk_resume_approves_every_pending_item(monkeypatch):
    from wren_agent.graph import route_after_approval
    from wren_agent.nodes.approval import human_approval, process_approval

    _patch_pause(monkeypatch, {"bulk": True, "approved": True, "comment": ""})
    state = _drift_state("ods_a", "ods_b", "ods_c")

    state = await human_approval(state)
    pending = [a for a in state["approval_items"] if a.status == ApprovalStatus.PENDING]
    assert pending == []
    assert all(a.status == ApprovalStatus.APPROVED for a in state["approval_items"])
    assert state["pending_human_input"] is False

    state = await process_approval(state)
    assert route_after_approval(state) == "design_ods"


@pytest.mark.asyncio
async def test_single_resume_still_approves_only_one_item(monkeypatch):
    from wren_agent.nodes.approval import human_approval

    state = _drift_state("ods_a", "ods_b")
    target_id = str(state["approval_items"][0].id)
    _patch_pause(monkeypatch, {"aid": target_id, "approved": True, "comment": ""})

    state = await human_approval(state)
    statuses = {a.artifact_id: a.status for a in state["approval_items"]}
    assert statuses["ods_a"] == ApprovalStatus.APPROVED
    assert statuses["ods_b"] == ApprovalStatus.PENDING
