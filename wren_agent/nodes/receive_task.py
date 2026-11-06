"""Task initialization node — registers task in PG, sets up initial state."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from wren_agent.state import AgentState

logger = logging.getLogger(__name__)

# PG table schema for tasks (created via migration):
#
# CREATE TABLE modeling_tasks (
#     task_id TEXT PRIMARY KEY,
#     thread_id TEXT NOT NULL UNIQUE,
#     status TEXT NOT NULL DEFAULT 'pending',
#     phase TEXT NOT NULL DEFAULT 'init',
#     modeling_goal TEXT NOT NULL,
#     source_databases JSONB NOT NULL DEFAULT '[]',
#     dw_target JSONB NOT NULL DEFAULT '{}',
#     state_json JSONB NOT NULL DEFAULT '{}',
#     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
# );
#
# CREATE TABLE task_audit_log (
#     id SERIAL PRIMARY KEY,
#     task_id TEXT NOT NULL REFERENCES modeling_tasks(task_id),
#     phase TEXT NOT NULL,
#     event_type TEXT NOT NULL,
#     details JSONB NOT NULL DEFAULT '{}',
#     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
# );
#
# CREATE INDEX idx_task_status ON modeling_tasks(status);
# CREATE INDEX idx_task_created ON modeling_tasks(created_at);
# CREATE INDEX idx_audit_task ON task_audit_log(task_id, created_at);

async def _register_task_in_pg(state: AgentState) -> None:
    """Insert task record into PostgreSQL."""
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO modeling_tasks (task_id, thread_id, status, phase, modeling_goal, mode, source_databases, dw_target, state_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (task_id) DO UPDATE SET
                status = EXCLUDED.status,
                phase = EXCLUDED.phase,
                mode = EXCLUDED.mode,
                modeling_goal = EXCLUDED.modeling_goal,
                source_databases = EXCLUDED.source_databases,
                dw_target = EXCLUDED.dw_target,
                updated_at = NOW()
            """,
            state["task_id"],
            state["thread_id"],
            state["status"].value if hasattr(state["status"], "value") else state["status"],
            state["phase"].value if hasattr(state["phase"], "value") else state["phase"],
            state["modeling_goal"],
            state.get("mode", "manual"),
            json.dumps([db.model_dump() if hasattr(db, "model_dump") else db for db in state["source_databases"]], default=str),
            json.dumps(state.get("dw_target", {}), default=str),
            json.dumps({}, default=str),  # Full state persisted by LangGraph Checkpointer
        )


async def _add_audit_log(state: AgentState, event_type: str, details: dict[str, Any]) -> None:
    """Write an audit log entry."""
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_audit_log (task_id, phase, event_type, details) VALUES ($1, $2, $3, $4)",
            state["task_id"],
            state["phase"].value if hasattr(state["phase"], "value") else state["phase"],
            event_type,
            json.dumps(details, default=str),
        )


async def receive_task(state: AgentState) -> AgentState:
    """Initialize a modeling task.

    Actions:
    1. Validate source databases configuration
    2. Register task in PG
    3. Write audit log entry
    4. Set initial status and phase
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    if not state["modeling_goal"]:
        state["errors"] = state.get("errors", []) + ["Modeling goal is required"]
        return state

    source_dbs = state.get("source_databases", [])
    if not source_dbs:
        state["errors"] = state.get("errors", []) + ["At least one source database is required"]
        return state

    # Validate each source DB has required fields
    for i, db in enumerate(source_dbs):
        if hasattr(db, "model_dump"):
            db = db.model_dump() if callable(db.model_dump) else db.__dict__
        missing = [k for k in ("name", "db_type", "host", "port", "database") if not db.get(k)]
        if missing:
            state["errors"] = state.get("errors", []) + [
                f"Source database [{i}] missing fields: {', '.join(missing)}"
            ]
            return state

    # Set initial status
    from wren_agent.models.task import TaskPhase, TaskStatus
    state["status"] = TaskStatus.EXPLORING
    state["phase"] = TaskPhase.INIT

    # Register in PG (non-fatal — checkpointer handles state)
    try:
        await _register_task_in_pg(state)
    except Exception:
        # PG unavailable in thread context — checkpointer handles state,
        # but stay visible: a swallowed failure here leaves the wrong
        # mode/goal in modeling_tasks for the task's whole lifetime.
        logger.warning("Task PG registration failed for %s",
                       state.get("task_id", "")[:8], exc_info=True)

    # Audit log (non-fatal)
    try:
        await _add_audit_log(state, "task_created", {
        "modeling_goal": state["modeling_goal"],
        "source_db_count": len(source_dbs),
    })
    except Exception:
        pass

    from wren_agent.tools.event_emitter import emit_status, emit_phase
    emit_status(state, "exploring", "pending")
    emit_phase(state, "init")

    return state
