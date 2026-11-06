"""Fire-and-forget WebSocket event emitter for LangGraph nodes.

Usage in any node:
    from wren_agent.tools.event_emitter import emit
    emit(state, "phase_change", {"phase": "design_ods"})

All emissions are non-blocking — if WS fails, the agent continues.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from typing import Any

from wren_agent.state import AgentState

logger = logging.getLogger(__name__)

# Event type constants (match WSEventType in api/websocket.py)
class Events:
    STATUS_CHANGE = "task_status_change"
    PHASE_CHANGE = "phase_change"
    THOUGHT = "thought_stream"
    SQL_EXEC = "sql_execution"
    SQL_RESULT = "sql_result"
    CLARIFICATION = "clarification_added"
    CLARIFICATION_ANSWERED = "clarification_answered"
    APPROVAL = "approval_added"
    APPROVAL_RESOLVED = "approval_resolved"
    VALIDATION = "validation_result"
    DAG_DEPLOY = "dag_deploy_status"
    DAG_LOG = "dag_execution_log"
    INTERRUPT = "interrupt"
    NODE_TIMELINE = "node_timeline"
    ERROR = "error"
    WARNING = "warning"
    LOG = "log_entry"


async def _broadcast(task_id: str, event_type: str, data: dict[str, Any]):
    """Internal: send to all WebSocket connections for a task."""
    try:
        # Import inline to avoid circular dependency
        from wren_agent.api.websocket import broadcast_to_task
        await broadcast_to_task(task_id, event_type, data)
    except Exception:
        pass  # Fire-and-forget


def emit(state: AgentState, event_type: str, data: dict[str, Any] | None = None):
    """Emit a WebSocket event for the current task. Non-blocking.

    Args:
        state: AgentState from the current node
        event_type: One of the Events constants
        data: Event-specific payload
    """
    task_id = state.get("task_id", "unknown")
    payload = data or {}

    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(_broadcast(task_id, event_type, payload))
    except RuntimeError:
        pass  # No event loop running


def emit_status(state: AgentState, new_status: str, old_status: str = ""):
    emit(state, Events.STATUS_CHANGE, {
        "old": old_status,
        "new": new_status,
    })


def emit_phase(state: AgentState, phase: str):
    emit(state, Events.PHASE_CHANGE, {"phase": phase})


def emit_sql(state: AgentState, sql: str, result: str = ""):
    emit(state, Events.SQL_EXEC, {"sql": sql[:500], "result": result[:200]})


def emit_clarification(
    state: AgentState,
    question: str,
    count: int,
    clarification_id: str = "",
    impact_level: str = "medium",
    clarification_type: str = "",
):
    emit(state, Events.CLARIFICATION, {
        "question": question[:200],
        "total_pending": count,
        "id": clarification_id,
        "impact_level": impact_level,
        "clarification_type": clarification_type,
    })


def emit_approval(
    state: AgentState,
    title: str,
    count: int,
    approval_id: str = "",
    artifact_type: str = "",
    description: str = "",
):
    emit(state, Events.APPROVAL, {
        "title": title[:200],
        "total_pending": count,
        "id": approval_id,
        "artifact_type": artifact_type,
        "description": description[:100],
    })


def emit_validation(state: AgentState, layer: str, passed: bool, details: str = ""):
    emit(state, Events.VALIDATION, {"layer": layer, "passed": passed, "details": details[:200]})


def emit_dag_deploy(state: AgentState, dag_name: str, status: str, dag_id: str = "", task_count: int = 0):
    emit(state, Events.DAG_DEPLOY, {
        "dag": dag_name,
        "status": status,
        "dag_id": dag_id,
        "task_count": task_count,
    })


def emit_sql_execution(
    state: AgentState,
    sql: str,
    target_table: str = "",
    duration_ms: float = 0,
):
    """Emit sql_execution event with structured data."""
    emit(state, Events.SQL_EXEC, {
        "sql_preview": sql[:300],
        "target_table": target_table,
        "duration_ms": round(duration_ms, 1),
        "node": state.get("current_node", ""),
    })


def emit_error(state: AgentState, message: str):
    emit(state, Events.ERROR, {"message": message[:500]})


def emit_interrupt(state: AgentState, interrupt_type: str, details: dict[str, Any] | None = None):
    """Emit an interrupt event when graph pauses for human input."""
    emit(state, Events.INTERRUPT, {
        "interrupt_type": interrupt_type,
        "status": state.get("status", ""),
        "details": details or {},
    })


def emit_warning(state: AgentState, message: str):
    emit(state, Events.WARNING, {"message": message[:500]})


def emit_node_timeline(
    state: AgentState,
    node_name: str,
    status: str,
    duration_ms: float = 0,
    error: str = "",
):
    """Emit a node_timeline event when a node completes or fails."""
    emit(state, "node_timeline", {
        "node": node_name,
        "status": status,
        "duration_ms": duration_ms,
        "error": error[:200] if error else "",
    })


def emit_thought(
    state: AgentState,
    prompt_summary: str,
    response_summary: str,
    duration_ms: float = 0,
):
    """Emit a thought_stream event capturing LLM prompt/response summaries."""
    emit(state, Events.THOUGHT, {
        "node": state.get("current_node", ""),
        "prompt_summary": prompt_summary[:300],
        "response_summary": response_summary[:300],
        "duration_ms": round(duration_ms, 1),
    })
