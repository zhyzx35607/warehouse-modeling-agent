"""WebSocket handler for real-time agent status streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

ws_router = APIRouter()

# Active connections: task_id -> list[WebSocket]
active_connections: dict[str, list[WebSocket]] = {}


class WSEventType:
    """Event types sent over WebSocket."""
    TASK_STATUS_CHANGE = "task_status_change"
    PHASE_CHANGE = "phase_change"
    THOUGHT_STREAM = "thought_stream"        # LLM reasoning stream
    SQL_EXECUTION = "sql_execution"           # SQL being executed
    SQL_RESULT = "sql_result"                 # SQL execution result
    CLARIFICATION_ADDED = "clarification_added"
    CLARIFICATION_ANSWERED = "clarification_answered"
    APPROVAL_ADDED = "approval_added"
    APPROVAL_RESOLVED = "approval_resolved"
    VALIDATION_RESULT = "validation_result"
    DAG_DEPLOY_STATUS = "dag_deploy_status"
    DAG_EXECUTION_LOG = "dag_execution_log"
    INTERRUPT = "interrupt"              # Human input needed (graph paused)
    NODE_TIMELINE = "node_timeline"      # Node execution completed/failed
    ERROR = "error"
    WARNING = "warning"
    LOG_ENTRY = "log_entry"                   # General audit log entry


async def broadcast_to_task(task_id: str, event_type: str, data: dict[str, Any]):
    """Send an event to all WebSocket connections watching a task."""
    if task_id not in active_connections:
        return

    from wren_agent.tools.redaction import sanitize_for_output

    message = json.dumps({
        "type": event_type,
        "task_id": task_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": sanitize_for_output(data),
    }, default=str)

    dead_connections = []
    for ws in active_connections[task_id]:
        try:
            await ws.send_text(message)
        except Exception:
            dead_connections.append(ws)

    for ws in dead_connections:
        active_connections[task_id].remove(ws)
    if not active_connections[task_id]:
        del active_connections[task_id]


# ── WebSocket route ─────────────────────────────────────────────


@ws_router.websocket("/ws/tasks/{task_id}")
async def task_websocket(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for real-time task monitoring.

    Client connects to: ws://host/ws/tasks/{task_id}
    Receives JSON events of various WSEventType types.
    """
    await websocket.accept()

    if task_id not in active_connections:
        active_connections[task_id] = []
    active_connections[task_id].append(websocket)

    try:
        # Send current state snapshot on connect
        await websocket.send_text(json.dumps({
            "type": "connected",
            "task_id": task_id,
            "message": f"Subscribed to task {task_id}",
        }))

        # Keep connection alive, listen for client messages
        # Client can send: ping, command (unused for now)
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    finally:
        if task_id in active_connections and websocket in active_connections[task_id]:
            active_connections[task_id].remove(websocket)
        # Drop the key once no watcher remains — otherwise the dict grows
        # by one entry per task forever.
        if task_id in active_connections and not active_connections[task_id]:
            del active_connections[task_id]
