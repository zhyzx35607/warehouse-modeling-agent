"""Stage operations API contract tests."""

import pytest
from fastapi import HTTPException

from wren_agent.api.routes import get_stage_metrics, get_task_lineage, get_task_stages, retry_task_stage


class _Pool:
    def __init__(self, *, fetch_sequence=None, fetchrow_sequence=None, fetchval_sequence=None):
        self.fetch_sequence = list(fetch_sequence or [])
        self.fetchrow_sequence = list(fetchrow_sequence or [])
        self.fetchval_sequence = list(fetchval_sequence or [])
        self.calls = []

    def acquire(self):
        return _Conn(self)


class _Conn:
    def __init__(self, pool): self.pool = pool
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return False
    def transaction(self): return _Transaction()
    async def fetch(self, sql, *args):
        self.pool.calls.append(("fetch", sql, args))
        return self.pool.fetch_sequence.pop(0)
    async def fetchrow(self, sql, *args):
        self.pool.calls.append(("fetchrow", sql, args))
        return self.pool.fetchrow_sequence.pop(0)
    async def fetchval(self, sql, *args):
        self.pool.calls.append(("fetchval", sql, args))
        return self.pool.fetchval_sequence.pop(0)
    async def execute(self, sql, *args):
        self.pool.calls.append(("execute", sql, args))
        return "UPDATE 1"


class _Transaction:
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return False


@pytest.mark.asyncio
async def test_stage_overview_returns_latest_contracts(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetchval_sequence=[1], fetch_sequence=[
        [{"stage": "exploration", "status": "succeeded"}],
        [{"stage": "exploration", "artifact_type": "source_catalog", "version": 2}],
        [{"table_identity": "src:public.orders", "status": "succeeded", "attempt": 1}],
    ])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    result = await get_task_stages("task-1")
    assert result["stages"][0]["status"] == "succeeded"
    assert result["artifacts"][0]["version"] == 2
    assert result["ods_tables"][0]["attempt"] == 1


@pytest.mark.asyncio
async def test_lineage_and_metrics_are_task_scoped(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetch_sequence=[
        [{"task_id": "task-1", "asset_kind": "dimension"}],
        [{"stage": "ods", "total": 10, "succeeded": 9, "partial_success": 1}],
    ])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    lineage = await get_task_lineage("task-1")
    metrics = await get_stage_metrics(window=20)
    assert lineage["assets"][0]["asset_kind"] == "dimension"
    assert metrics["window"] == 20
    assert pool.calls[1][2] == (20,)


@pytest.mark.asyncio
async def test_ods_retry_rejects_non_transient_failure(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetchrow_sequence=[
        {"status": "failed", "attempt": 1},
        {"status": "failed", "attempt": 1, "error_type": "quality", "manifest_version": 1},
    ], fetchval_sequence=[None])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    with pytest.raises(HTTPException) as exc:
        await retry_task_stage("task-1", "ods", "src:public.orders")
    assert exc.value.status_code == 409
    assert "not retryable" in exc.value.detail


@pytest.mark.asyncio
async def test_ods_retry_requires_deployed_mapping_before_new_attempt(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(
        fetchrow_sequence=[
            {"status": "failed", "attempt": 1},
            {"status": "failed", "attempt": 1, "error_type": "transient",
             "manifest_version": 1, "required": True, "sync_mode": "full"},
            None,
        ],
        fetchval_sequence=[None, {"tables": [
            {"table_identity": "src:public.orders", "ods_table": "ods_orders"},
        ]}],
    )
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    with pytest.raises(HTTPException) as exc:
        await retry_task_stage("task-1", "ods", "src:public.orders")
    assert exc.value.status_code == 409
    assert not any(call[0] == "execute" for call in pool.calls)


@pytest.mark.asyncio
async def test_ods_retry_rejects_empty_manifest_target_before_dag_lookup(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(
        fetchrow_sequence=[
            {"status": "failed", "attempt": 1},
            {"status": "failed", "attempt": 1, "error_type": "transient",
             "manifest_version": 1, "required": True, "sync_mode": "full"},
        ],
        fetchval_sequence=[None, {"tables": [
            {"table_identity": "src:public.orders", "ods_table": ""},
        ]}],
    )
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    with pytest.raises(HTTPException) as exc:
        await retry_task_stage("task-1", "ods", "src:public.orders")
    assert exc.value.status_code == 409
    assert "deployment manifest" in exc.value.detail
    assert len([call for call in pool.calls if call[0] == "fetchrow"]) == 2
    assert not any(call[0] == "execute" for call in pool.calls)


@pytest.mark.asyncio
async def test_ods_retry_serializes_requests_with_table_scoped_lock(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetchrow_sequence=[
        {"status": "failed", "attempt": 1},
        {"status": "running", "attempt": 2, "error_type": "", "manifest_version": 1},
    ], fetchval_sequence=[None])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)
    with pytest.raises(HTTPException) as exc:
        await retry_task_stage("task-1", "ods", "src:public.orders")
    assert exc.value.status_code == 409
    lock_call = next(call for call in pool.calls if "pg_advisory_xact_lock" in call[1])
    assert lock_call[2] == ("ods-retry:task-1:src:public.orders",)
    assert not any(call[0] == "execute" for call in pool.calls)
