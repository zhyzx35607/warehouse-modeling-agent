"""Node tracing middleware for LangGraph nodes.

Wraps every node with start/end timing, event emission,
and node_timeline recording in AgentState.

Usage in graph.py:
    from wren_agent.tools.node_tracer import wrap_node_with_trace
    _design_ods = wrap_node_with_trace(
        wrap_node_with_recovery(design_ods, "design_ods"), "design_ods"
    )
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from wren_agent.state import AgentState

logger = logging.getLogger(__name__)


def _persist_runtime_snapshot(state: AgentState) -> None:
    """Fire-and-forget: upsert node_timeline/audit_log to PG mid-run.

    Lets the dashboard rebuild both timelines after a page switch — the bare
    graph has no checkpointer, so without this the data only lives in memory.
    Also mirrors `status`/`phase` onto modeling_tasks so the task list shows
    real progress (task d28431de: every node completed yet modeling_tasks
    stayed `exploring/init` for the whole 74-minute run — the frontend and
    any monitoring could not tell the task was mid-design).
    """
    try:
        import asyncio

        from wren_agent.tools.pg_store import (
            persist_runtime_lifecycle,
            persist_runtime_task_status,
        )

        loop = asyncio.get_running_loop()
        loop.create_task(persist_runtime_lifecycle(
            state.get("task_id", ""),
            list(state.get("node_timeline", [])),
            list(state.get("audit_log", [])),
            list(state.get("errors", [])),
            list(state.get("warnings", [])),
        ))
        status = state.get("status")
        if status is not None:
            phase = str(state.get("phase", "") or "")
            loop.create_task(persist_runtime_task_status(
                state.get("task_id", ""), str(status), phase,
            ))
    except Exception:
        pass  # Observability must never break the agent


def wrap_node_with_trace(
    node_fn: Callable[[AgentState], Any],
    node_name: str,
) -> Callable[[AgentState], Any]:
    """Wrap a LangGraph node function with execution tracing.

    Records:
    - Sets state["current_node"] so the frontend always knows where we are
    - Emits a phase_change WebSocket event for real-time tracking
    - On completion: appends {node, status:"completed", duration_ms, timestamp}
      to state["node_timeline"]
    - On failure: appends {node, status:"failed", error, timestamp}
      and re-raises

    Args:
        node_fn: The async node function (state) -> state
        node_name: Human-readable node name (e.g. "design_ods")

    Returns:
        Traced async function with the same signature
    """

    async def traced(state: AgentState) -> AgentState:
        t0 = time.monotonic()
        now = datetime.now(timezone.utc)

        # Set current node for frontend visibility
        state["current_node"] = node_name
        logger.info(
            "[%s] Node '%s' started",
            state.get("task_id", "?")[:8], node_name,
        )
        # Persist mid-run so the dashboard can show the active node while a
        # long explore/design step is still executing (checkpointer only
        # advances between nodes).
        _persist_runtime_snapshot(state)

        # Emit WebSocket phase change event (fire-and-forget)
        try:
            from wren_agent.tools.event_emitter import emit_phase
            emit_phase(state, node_name)
        except Exception:
            pass  # Non-blocking

        # Set LLM context for thought_stream capture
        try:
            from wren_agent.tools.llm_context import (
                set_current_node,
                set_current_skills,
                set_current_task_id,
            )
            task_id = state.get("task_id", "")
            set_current_task_id(task_id)
            set_current_node(node_name)
            set_current_skills(())  # Reset — build_system_prompt fills it per call
        except Exception:
            pass

        try:
            result = await node_fn(state)
            duration_ms = (time.monotonic() - t0) * 1000

            timeline_entry = {
                "node": node_name,
                "status": "completed",
                "duration_ms": round(duration_ms, 1),
                "timestamp": now.isoformat(),
            }
            # Latest-wins per node: crash recovery re-runs nodes, and naive
            # appending makes the UI show e.g. "78/29 completed" (task
            # 6521caff restarted 3 times and tripled every entry).
            state["node_timeline"] = [
                e for e in state.get("node_timeline", [])
                if e.get("node") != node_name
            ]
            state["node_timeline"].append(timeline_entry)

            logger.info(
                f"[{state.get('task_id', '?')[:8]}] Node '{node_name}' "
                f"completed in {duration_ms:.0f}ms"
            )

            # Emit node_timeline event for real-time UI update
            try:
                from wren_agent.tools.event_emitter import emit_node_timeline
                emit_node_timeline(state, node_name, "completed", round(duration_ms, 1))
            except Exception:
                pass

            _persist_runtime_snapshot(state)

            return result

        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000

            timeline_entry = {
                "node": node_name,
                "status": "failed",
                "duration_ms": round(duration_ms, 1),
                "error": str(e)[:200],
                "timestamp": now.isoformat(),
            }
            state["node_timeline"] = [
                x for x in state.get("node_timeline", [])
                if x.get("node") != node_name
            ]
            state["node_timeline"].append(timeline_entry)

            logger.error(
                f"[{state.get('task_id', '?')[:8]}] Node '{node_name}' "
                f"FAILED after {duration_ms:.0f}ms: {e}"
            )

            # Emit node_timeline failure event
            try:
                from wren_agent.tools.event_emitter import emit_node_timeline
                emit_node_timeline(
                    state, node_name, "failed",
                    round(duration_ms, 1), str(e)[:200]
                )
            except Exception:
                pass

            _persist_runtime_snapshot(state)

            raise

    # Preserve original name for debugging
    traced.__name__ = f"traced_{node_name}"
    return traced
