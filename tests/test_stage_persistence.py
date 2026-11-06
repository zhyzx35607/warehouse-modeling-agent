"""Stage contract and PostgreSQL persistence tests."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from wren_agent.models.stage import (
    ModelingStage,
    ModelingStageRun,
    StageArtifact,
    StageArtifactType,
    StageStatus,
)
from wren_agent.tools import pg_store


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Conn:
    def __init__(self, latest=0):
        self.latest = latest
        self.calls = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "SELECT 1"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.latest

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "stage_artifacts" in query and "INSERT" in query:
            return {"task_id": args[0], "stage": args[1],
                    "artifact_type": args[2], "version": args[3],
                    "supersedes_version": args[6]}
        if "modeling_stages" in query:
            return {"task_id": args[0], "stage": args[1], "status": args[3]}
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_terminal_stage_requires_finished_at():
    with pytest.raises(ValidationError, match="finished_at"):
        ModelingStageRun(
            task_id="task-1", stage=ModelingStage.ODS,
            thread_id="task-1:ods", status=StageStatus.SUCCEEDED,
        )


def test_stage_artifact_requires_sequential_version_chain():
    with pytest.raises(ValidationError, match="immediately previous"):
        StageArtifact(
            task_id="task-1", stage=ModelingStage.ODS,
            artifact_type=StageArtifactType.ODS_MANIFEST,
            version=3, supersedes_version=1, payload={},
        )


@pytest.mark.asyncio
async def test_upsert_modeling_stage(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _Pool(conn))
    result = await pg_store.upsert_modeling_stage(ModelingStageRun(
        task_id="task-1", stage=ModelingStage.ODS,
        thread_id="task-1:ods", status=StageStatus.RUNNING, attempt=1,
        started_at=datetime.now(timezone.utc),
    ))
    assert result == {"task_id": "task-1", "stage": "ods", "status": "running"}
    assert "ON CONFLICT (task_id, stage)" in conn.calls[0][1]


@pytest.mark.asyncio
async def test_create_stage_artifact_appends_version(monkeypatch):
    conn = _Conn(latest=2)
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _Pool(conn))
    result = await pg_store.create_stage_artifact(
        "task-1", "ods", "ods_manifest", {"tables": []}, "sha256:x",
    )
    assert result["version"] == 3
    assert result["supersedes_version"] == 2
    assert "pg_advisory_xact_lock" in conn.calls[0][1]
