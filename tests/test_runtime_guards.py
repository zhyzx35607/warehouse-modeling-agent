"""Runtime lifecycle and API guard regression tests."""

import json

import pytest


@pytest.mark.asyncio
async def test_websocket_broadcast_uses_utc_timestamp():
    from wren_agent.api import websocket as ws_module

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_text(self, message):
            self.messages.append(message)

    task_id = "runtime-guard-task"
    socket = FakeWebSocket()
    ws_module.active_connections[task_id] = [socket]
    try:
        await ws_module.broadcast_to_task(task_id, "phase_change", {"phase": "init"})
    finally:
        ws_module.active_connections.pop(task_id, None)

    payload = json.loads(socket.messages[0])
    assert payload["task_id"] == task_id
    assert payload["timestamp"].endswith("+00:00")
    assert payload["data"] == {"phase": "init"}


@pytest.mark.asyncio
async def test_websocket_broadcast_redacts_sensitive_payload(monkeypatch):
    from wren_agent.api import websocket as ws_module

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_text(self, message):
            self.messages.append(message)

    monkeypatch.setattr(
        "wren_agent.tools.redaction._configured_secret_values",
        lambda: ["configured-secret-value"],
    )
    task_id = "runtime-sensitive-task"
    socket = FakeWebSocket()
    ws_module.active_connections[task_id] = [socket]
    try:
        await ws_module.broadcast_to_task(task_id, "error", {
            "password": "plain-password",
            "message": (
                "api_key=configured-secret-value "
                "email=user@example.com phone=13800000000"
            ),
        })
    finally:
        ws_module.active_connections.pop(task_id, None)

    serialized = socket.messages[0]
    assert "plain-password" not in serialized
    assert "configured-secret-value" not in serialized
    assert "user@example.com" not in serialized
    assert "13800000000" not in serialized


@pytest.mark.asyncio
async def test_pg_pool_initialization_is_idempotent(monkeypatch):
    from wren_agent.tools import pg_store

    existing_pool = object()
    monkeypatch.setattr(pg_store, "_pool", existing_pool)

    async def fail_create_pool(**kwargs):
        raise AssertionError("create_pool must not run for an initialized pool")

    monkeypatch.setattr(pg_store.asyncpg, "create_pool", fail_create_pool)
    assert await pg_store.init_pg_pool() is existing_pool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_status", "expected_status"),
    [("ok", 200), ("degraded", 503)],
)
async def test_health_http_status_matches_dependency_state(
    monkeypatch, aggregate_status, expected_status
):
    from wren_agent.api import app as app_module
    from wren_agent.api import health_check as health_module

    async def fake_check_all():
        return {"status": aggregate_status, "services": {}}

    monkeypatch.setattr(health_module, "check_all", fake_check_all)
    response = await app_module.health_check()

    assert response.status_code == expected_status
    assert json.loads(response.body)["status"] == aggregate_status


@pytest.mark.asyncio
async def test_cancel_task_stops_tracked_coroutine(monkeypatch):
    import asyncio

    from wren_agent.engine import AgentRunner

    runner = AgentRunner()
    started = asyncio.Event()
    updates = []

    async def worker():
        started.set()
        await asyncio.Event().wait()

    async def record_update(task_id, status, phase, mode=None):
        updates.append((task_id, status, phase))

    monkeypatch.setattr(runner, "_update_task_pg", record_update)
    task = asyncio.create_task(worker())
    runner._track_task("task-1", task)
    await started.wait()

    assert await runner.cancel_task("task-1") is True
    assert task.cancelled()
    assert ("task-1", "cancelled", "") in updates
    assert "task-1" not in runner._active_tasks


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_false():
    from wren_agent.engine import AgentRunner

    runner = AgentRunner()
    assert await runner.cancel_task("missing") is False


@pytest.mark.asyncio
async def test_cancel_stale_running_task_updates_persisted_status(monkeypatch):
    from wren_agent.engine import AgentRunner

    runner = AgentRunner()
    updates = []

    async def stale_task_state(task_id):
        return {"task_id": task_id, "status": "running", "phase": "design_dws"}

    async def record_update(task_id, status, phase, mode=None):
        updates.append((task_id, status, phase))

    async def ignore_broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "get_task_state", stale_task_state)
    monkeypatch.setattr(runner, "_update_task_pg", record_update)
    monkeypatch.setattr(
        "wren_agent.api.websocket.broadcast_to_task", ignore_broadcast
    )

    assert await runner.cancel_task("stale-task") is True
    assert updates == [("stale-task", "cancelled", "")]


@pytest.mark.asyncio
async def test_cancel_terminal_task_is_rejected(monkeypatch):
    from wren_agent.engine import AgentRunner

    runner = AgentRunner()

    async def completed_task_state(task_id):
        return {"task_id": task_id, "status": "completed", "phase": "report"}

    monkeypatch.setattr(runner, "get_task_state", completed_task_state)
    assert await runner.cancel_task("done-task") is False


@pytest.mark.asyncio
async def test_shutdown_cancels_all_tracked_tasks():
    import asyncio

    from wren_agent.engine import AgentRunner

    runner = AgentRunner()

    async def worker():
        await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    runner._track_task("task-1", task)

    await runner.shutdown()

    assert task.cancelled()
    assert not runner._active_tasks
