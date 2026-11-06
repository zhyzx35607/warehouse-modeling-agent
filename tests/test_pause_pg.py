"""Cross-engine pause/resume tests (migration 010, task_pauses).

The pause nodes (wait_human / human_approval) now register in PG and poll it
while waiting, so a decision made on ANOTHER engine instance resumes the
task; the in-process asyncio.Event is only a fast path.
"""

import asyncio
import json

import pytest

from wren_agent.engine import wait_for_resume
from wren_agent.tools.pg_store import get_task_pause, resume_task


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_wait_for_resume_timeout_returns_empty(monkeypatch):
    async def fake_get(task_id):
        return {"status": "paused", "payload": {}}

    monkeypatch.setattr("wren_agent.tools.pg_store.get_task_pause", fake_get)
    ev = asyncio.Event()
    payload = await wait_for_resume("t1", ev, timeout=0.1)
    assert payload == {}


@pytest.mark.asyncio
async def test_wait_for_resume_cross_engine_poll(monkeypatch):
    """Decision recorded in PG by another engine — no local event set."""
    calls = {"n": 0}

    async def fake_get(task_id):
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"status": "resumed", "payload": {"cid": "c1", "answer": "部分支撑"}}
        return {"status": "paused", "payload": {}}

    monkeypatch.setattr("wren_agent.tools.pg_store.get_task_pause", fake_get)
    ev = asyncio.Event()  # never set locally — the cross-engine path
    payload = await wait_for_resume("t1", ev, timeout=30)
    assert payload == {"cid": "c1", "answer": "部分支撑"}
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_wait_for_resume_in_process_fast_path(monkeypatch):
    async def fake_get(task_id):
        return {"status": "resumed", "payload": {"aid": "a1", "approved": True}}

    monkeypatch.setattr("wren_agent.tools.pg_store.get_task_pause", fake_get)
    ev = asyncio.Event()
    ev.set()  # same-engine wakeup
    payload = await wait_for_resume("t1", ev, timeout=30)
    assert payload == {"aid": "a1", "approved": True}


@pytest.mark.asyncio
async def test_resume_without_pause_row_times_out(monkeypatch):
    async def fake_get(task_id):
        return None  # never paused

    monkeypatch.setattr("wren_agent.tools.pg_store.get_task_pause", fake_get)
    ev = asyncio.Event()
    payload = await wait_for_resume("t1", ev, timeout=0.1)
    assert payload == {}


@pytest.mark.asyncio
async def test_resume_task_serializes_jsonb_payload(monkeypatch):
    class Conn:
        async def execute(self, _query, task_id, payload):
            assert task_id == "t1"
            assert isinstance(payload, str)
            assert json.loads(payload) == {"cid": "c1", "answer": "部分支撑"}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: _Pool(Conn())
    )
    await resume_task("t1", {"cid": "c1", "answer": "部分支撑"})


@pytest.mark.asyncio
async def test_get_task_pause_parses_jsonb_string(monkeypatch):
    class Conn:
        async def fetchrow(self, _query, task_id):
            assert task_id == "t1"
            return {
                "task_id": "t1",
                "pause_type": "clarification",
                "status": "resumed",
                "payload": '{"cid": "c1", "answer": "部分支撑"}',
            }

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: _Pool(Conn())
    )
    row = await get_task_pause("t1")
    assert row["payload"] == {"cid": "c1", "answer": "部分支撑"}
