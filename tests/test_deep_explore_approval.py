"""Deep exploration must never run without explicit authorization."""

import pytest

from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.state import create_initial_state


APPROVAL_ID = "49469718-bc57-5132-8194-f1eea5a91964"


def _state(mode: str = "manual"):
    state = create_initial_state("deep-task", "goal", [], {}, mode=mode)
    state["explorations"] = {
        "source-1": SchemaExploration(
            source_db_id="source-1",
            tables=[TableSchema(
                name="orders",
                db_type="mysql",
                fields=[FieldSchema(name="status", data_type="varchar(16)")],
                row_count_estimate=200_000,
            )],
        )
    }
    return state


def _patch_pause_dependencies(monkeypatch, payload):
    async def noop(*_args, **_kwargs):
        return None

    async def no_decision(*_args, **_kwargs):
        return None

    async def resume(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(
        "wren_agent.nodes.explore._persist_deep_explore_approval", noop,
    )
    monkeypatch.setattr(
        "wren_agent.nodes.explore._load_deep_explore_decision", no_decision,
    )
    monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", noop)
    monkeypatch.setattr("wren_agent.tools.pg_store.clear_task_pause", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)
    monkeypatch.setattr("wren_agent.engine.persist_approval_response", noop)
    monkeypatch.setattr("wren_agent.engine.wait_for_resume", resume)
    monkeypatch.setattr(
        "wren_agent.tools.event_emitter.emit_interrupt", lambda *_a, **_k: None,
    )


@pytest.mark.asyncio
async def test_manual_approval_keeps_queries_for_deep_explore(monkeypatch):
    from wren_agent.nodes.explore import human_confirm_deep

    state = _state()
    _patch_pause_dependencies(monkeypatch, {
        "aid": APPROVAL_ID, "approved": True,
    })

    result = await human_confirm_deep(state)

    assert result["deep_explore_approval_id"] == APPROVAL_ID
    assert result["deep_explore_decision"] == "approved"
    assert len(result["deep_explore_pending"]) == 1
    assert result["pending_human_input"] is False


@pytest.mark.asyncio
async def test_manual_rejection_skips_all_deep_queries(monkeypatch):
    from wren_agent.nodes.explore import human_confirm_deep

    state = _state()
    _patch_pause_dependencies(monkeypatch, {
        "aid": APPROVAL_ID, "approved": False,
    })

    result = await human_confirm_deep(state)

    assert result["deep_explore_decision"] == "rejected"
    assert result["deep_explore_pending"] == []


@pytest.mark.asyncio
async def test_timeout_skips_queries_instead_of_falling_through(monkeypatch):
    from wren_agent.nodes.explore import human_confirm_deep

    state = _state()
    _patch_pause_dependencies(monkeypatch, {})
    persisted = []

    async def capture_timeout(task_id, approval_id, approved, comment):
        persisted.append((task_id, approval_id, approved, comment))

    monkeypatch.setattr(
        "wren_agent.engine.persist_approval_response", capture_timeout,
    )

    result = await human_confirm_deep(state)

    assert result["deep_explore_decision"] == "timeout"
    assert result["deep_explore_pending"] == []
    assert any("timed out" in warning for warning in result["warnings"])
    assert persisted == [
        ("deep-task", APPROVAL_ID, False, "[timeout] 深度探索授权超时，已跳过查询")
    ]


@pytest.mark.asyncio
async def test_auto_mode_skips_without_registering_pause(monkeypatch):
    from wren_agent.nodes.explore import human_confirm_deep

    async def must_not_pause(*_args, **_kwargs):
        pytest.fail("auto mode attempted to register a deep-explore approval")

    monkeypatch.setattr(
        "wren_agent.nodes.explore._persist_deep_explore_approval", must_not_pause,
    )

    result = await human_confirm_deep(_state(mode="auto"))

    assert result["deep_explore_decision"] == "skipped_auto"
    assert result["deep_explore_pending"] == []


@pytest.mark.asyncio
async def test_persisted_cross_engine_decision_avoids_wait(monkeypatch):
    from wren_agent.nodes.explore import human_confirm_deep

    async def noop(*_args, **_kwargs):
        return None

    async def approved(*_args, **_kwargs):
        return "approved"

    async def must_not_wait(*_args, **_kwargs):
        pytest.fail("persisted approval should be consumed before pausing")

    monkeypatch.setattr(
        "wren_agent.nodes.explore._persist_deep_explore_approval", noop,
    )
    monkeypatch.setattr(
        "wren_agent.nodes.explore._load_deep_explore_decision", approved,
    )
    monkeypatch.setattr("wren_agent.engine.wait_for_resume", must_not_wait)
    monkeypatch.setattr("wren_agent.tools.pg_store.clear_task_pause", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)
    monkeypatch.setattr("wren_agent.engine.persist_approval_response", noop)

    result = await human_confirm_deep(_state())

    assert result["deep_explore_decision"] == "approved"
    assert len(result["deep_explore_pending"]) == 1
