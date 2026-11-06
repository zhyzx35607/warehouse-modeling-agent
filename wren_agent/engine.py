"""Agent Runner — bridges LangGraph to FastAPI.

Compiles the graph WITH a PG checkpointer (crash recovery) + manual PG
state sync for the dashboard. Pauses at HITL nodes using asyncio.Event.
"""

import asyncio
import json
import logging
import uuid
from datetime import timezone
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from wren_agent.config import settings
from wren_agent.graph import build_modeling_agent_graph
from wren_agent.models.task import TaskStatus
from wren_agent.state import AgentState, create_initial_state

logger = logging.getLogger(__name__)

MAX_RECOVERY_ATTEMPTS = 3
LIVE_TASK_STATUSES = {
    "pending", "running", "exploring", "designing", "validating",
    "deploying", "executing", "grilling", "awaiting_approval",
}

# Active pauses: task_id -> asyncio.Event
_task_pauses: dict[str, asyncio.Event] = {}


def _status_str(value: Any) -> str:
    """Normalize a TaskStatus/TaskPhase enum (or plain str) to its value.

    str(TaskStatus.FAILED) is "TaskStatus.FAILED" — writing that to PG breaks
    every status filter, so unwrap .value first.
    """
    return value.value if hasattr(value, "value") else str(value)


def _is_validation_failure_state(state: dict[str, Any]) -> bool:
    """Return True when FAILED means a rejected design, not a process crash."""
    if state.get("failure_kind") == "validation" or state.get("validation_blocking"):
        return True
    return any(
        "Critical design invariants failed" in str(message)
        or "no SQL artifacts were generated" in str(message)
        or "Missing design plan" in str(message)
        for message in state.get("errors", [])
    )


async def wait_for_resume(
    task_id: str,
    pause_ev: asyncio.Event,
    timeout: float = 1800,
) -> dict:
    """Wait for a human resume — in-process event OR cross-engine PG poll.

    The paused node registers its row via pg_store.pause_task() before
    calling this; resume_with_* writes the decision to that row. This loop
    wakes on the in-process event (fast path) and also polls PG every 5s so
    a decision made on ANOTHER engine instance resumes the task here.
    Returns the resume payload ({} on timeout).
    """
    from wren_agent.tools.pg_store import get_task_pause

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(f"[{task_id[:8]}] Human wait timed out after {timeout:.0f}s")
            return {}
        try:
            await asyncio.wait_for(pause_ev.wait(), timeout=min(5.0, remaining))
        except asyncio.TimeoutError:
            pass  # poll PG below — the event may have fired on another engine
        try:
            row = await get_task_pause(task_id)
            if row and row["status"] == "resumed":
                return row["payload"] or {}
        except Exception:
            logger.debug("pause poll failed", exc_info=True)
        try:
            from wren_agent.tools.pg_store import get_pg_pool
            pool = get_pg_pool()
            async with pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT status FROM modeling_tasks WHERE task_id=$1", task_id
                )
            if status == "cancelled":
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("cancel poll failed", exc_info=True)


class AgentRunner:
    """Manages LangGraph agent lifecycle per task."""

    def __init__(self):
        self._graph = None
        self._checkpointer: AsyncPostgresSaver | None = None
        self._cp_pool: AsyncConnectionPool | None = None
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def startup(self):
        from wren_agent.config import settings as s

        # 1. Initialize shared checkpointer (one instance for the whole process).
        # NOTE: AsyncPostgresSaver.from_conn_string() is an @asynccontextmanager —
        # calling it bare returns the context manager, not a saver, and its
        # connection dies on scope exit. Own the pool instead so the saver
        # lives as long as the process; connection kwargs mirror
        # from_conn_string(): autocommit + no prepared statements + dict rows.
        self._cp_pool = AsyncConnectionPool(
            s.pg.safe_dsn,  # sslmode=disable — OpenSSL applink crash guard
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await self._cp_pool.open()
        self._checkpointer = AsyncPostgresSaver(self._cp_pool)
        await self._checkpointer.setup()

        # 2. Compile graph WITH checkpointer for crash recovery
        self._graph = build_modeling_agent_graph(checkpointer=self._checkpointer)

        logger.info("AgentRunner: graph initialized WITH checkpointer for crash recovery")

    async def shutdown(self):
        for ev in _task_pauses.values():
            ev.set()
        active = list(self._active_tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._cp_pool is not None:
            try:
                await self._cp_pool.close()
            except Exception as e:
                logger.debug(f"Checkpointer pool close failed: {e}")

    def run_task_background(self, state: AgentState):
        task_id = state["task_id"]
        task = asyncio.create_task(
            self._run_task(state), name=f"agent_{task_id[:8]}"
        )
        self._track_task(task_id, task)
        logger.info(f"Agent task started: {task_id[:8]} — '{state['modeling_goal'][:60]}'")

    def _track_task(self, task_id: str, task: asyncio.Task) -> None:
        """Track a background task until it completes or is cancelled."""
        self._active_tasks[task_id] = task

        def forget(done_task: asyncio.Task) -> None:
            if self._active_tasks.get(task_id) is done_task:
                self._active_tasks.pop(task_id, None)

        task.add_done_callback(forget)

    async def _run_task(self, state: AgentState):
        task_id = state["task_id"]
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}

        final_state = None
        last_state: AgentState = state  # Best-known state — updated each superstep
        try:
            await self._update_task_pg(task_id, "running", "init", state.get("mode"))
            # Stream state values so a mid-run crash still leaves us the last
            # good snapshot (node_timeline, audit_log, errors, ...) to persist.
            async for chunk in self._graph.astream(state, config, stream_mode="values"):
                last_state = chunk
                task_row = await self.get_task_state(task_id)
                if task_row and task_row.get("status") == "cancelled":
                    raise asyncio.CancelledError
            final_state = last_state
            await self._update_task_pg(task_id,
                _status_str(final_state.get("status", "completed")),
                _status_str(final_state.get("phase", "report")))
            logger.info(f"[{task_id[:8]}] Agent completed")
        except asyncio.CancelledError:
            await self._update_task_pg(
                task_id, "cancelled", str(last_state.get("phase", ""))
            )
            logger.info(f"[{task_id[:8]}] Agent cancelled")
            raise
        except Exception as e:
            await self._update_task_pg(task_id, "failed", str(last_state.get("phase", "")))
            logger.error(f"[{task_id[:8]}] Agent failed: {e}")
            # Tell the dashboard the task died — otherwise it keeps showing
            # the last live status and silently stops updating.
            try:
                from wren_agent.api.websocket import broadcast_to_task
                await broadcast_to_task(task_id, "task_status_change", {
                    "old": str(last_state.get("status", "")), "new": "failed",
                })
                await broadcast_to_task(task_id, "error", {
                    "message": f"Agent failed: {str(e)[:300]}",
                })
            except Exception:
                pass
        finally:
            # Persist artifacts to dedicated PG tables for fast API reads.
            # On failure, persist the last streamed state — NOT the initial
            # empty state, which would wipe runtime data with NULLs.
            persist_state = final_state if final_state is not None else last_state
            try:
                from wren_agent.tools.pg_store import persist_task_artifacts
                await persist_task_artifacts(persist_state)
            except Exception as e:
                logger.warning(f"[{task_id[:8]}] Artifact persistence failed: {e}")

    # ── Crash recovery ─────────────────────────────────────────────

    async def recover_task(self, task_id: str) -> dict:
        """Recover a crashed task from the last LangGraph checkpoint.

        Called by POST /api/v1/tasks/{task_id}/recover.
        Returns {status, message} — the caller maps status to HTTP code.
        """
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}

        # 1. Verify task exists and is in a recoverable state
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            return {"status": "already_running", "message": "Task is still running"}

        pg_state = await self.get_task_state(task_id)
        if not pg_state:
            return {"status": "not_found", "message": f"Task '{task_id}' not found"}

        status = pg_state.get("status", "")
        if status == "completed":
            return {"status": "completed", "message": "Task already completed"}
        if status == "running":
            return {"status": "already_running", "message": "Task is still running"}
        # Paused states (grilling / awaiting_approval) become recoverable when
        # the owning engine died: recovery re-enters the pause node, which
        # re-registers in task_pauses and consumes any decision already
        # recorded via PG (migration 010).
        if status in ("grilling", "awaiting_approval"):
            pass  # fall through to recovery
        elif (
            status == "designing"
            and "HUMAN_APPROVAL" in str(pg_state.get("phase") or "")
        ):
            # After a partial drift decision PG is flipped to designing before
            # interrupt_before re-enters human_approval (task 9fcfcdbb).
            pass
        elif status not in ("failed", "cancelled"):
            return {"status": "not_recoverable",
                    "message": f"Status '{status}' is not recoverable"}

        # 2. Check if a checkpoint exists for this thread
        if self._checkpointer is None:
            return {"status": "no_checkpointer",
                    "message": "Checkpointer not initialized"}

        checkpoint = await self._checkpointer.aget_tuple(config)
        if checkpoint is None:
            return {"status": "no_checkpoint",
                    "message": "No checkpoint found for recovery"}

        # 3. Read recovery metadata from the checkpoint. Validation failures
        # are deterministic design rejections, not crashes: replaying a
        # terminal checkpoint restarts the graph and produces the same invalid
        # design, so they must be corrected/re-run instead of "recovered".
        channel_values = checkpoint.checkpoint.get("channel_values", {})
        if _is_validation_failure_state(channel_values):
            return {
                "status": "validation_failed",
                "message": (
                    "Task failed design validation; checkpoint recovery is not applicable. "
                    "Correct the design issues and run the task again."
                ),
            }

        recovery_count = channel_values.get("recovery_count", 0)
        if recovery_count >= MAX_RECOVERY_ATTEMPTS:
            return {
                "status": "recovery_limit_reached",
                "message": f"Recovery limit reached ({MAX_RECOVERY_ATTEMPTS} attempts)",
            }

        # 4. Clear stale asyncio.Event from the crashed asyncio task
        old_ev = _task_pauses.pop(task_id, None)
        if old_ev:
            old_ev.set()

        # 4b. A TERMINAL checkpoint (the failed run finished the graph, e.g.
        # through generate_report) restarts the whole graph from __start__.
        # The checkpointed failure state must NOT leak into the fresh run:
        # `error_classifications` alone re-opens execution approval items
        # (human_approval derives them from state), which auto mode then
        # auto-rejects as a "new" config error and fails the re-run
        # (task a1af4bed: recovery attempt 2 redesigned cleanly, then died at
        # human_approval on the previous run's stale `DAG 执行错误` item).
        reset_state: dict[str, Any] = {}
        if not checkpoint.checkpoint.get("next"):
            reset_state = {
                "errors": [],
                "error_classifications": [],
                "execution_results": [],
                "validation_results": [],
                "approval_items": [],
                "auto_approval_rounds": 0,
                "error_recovery_rounds": 0,
                "deploy_failure_count": 0,
                "deploy_succeeded": False,
                "ds_project_code": None,
                "ds_data_sources": [],
                "ds_dags": [],
                "failure_kind": "",
            }

        # 5. Update PG status and launch recovery in background
        await self._update_task_pg(
            task_id, "running", str(channel_values.get("phase", ""))
        )
        try:
            from wren_agent.api.websocket import broadcast_to_task
            await broadcast_to_task(task_id, "task_status_change", {
                "old": status,
                "new": "running",
            })
        except Exception:
            pass
        task = asyncio.create_task(
            self._run_recovery(task_id, config, recovery_count, reset_state),
            name=f"recover_{task_id[:8]}",
        )
        self._track_task(task_id, task)
        logger.info(
            f"[{task_id[:8]}] Recovery started (attempt {recovery_count + 1})"
        )
        return {"status": "recovering",
                "message": f"Recovery attempt {recovery_count + 1}"}

    async def _run_recovery(
        self, task_id: str, config: dict, recovery_count: int,
        reset_state: dict[str, Any] | None = None,
    ):
        """Execute recovery: resume graph from last checkpoint.

        Passes a partial state update (recovery_count increment) to LangGraph,
        which merges it with the checkpointed state and continues from the
        pending node. `reset_state` (terminal-checkpoint restarts) overwrites
        the stale failure channels so the fresh run starts clean.
        """
        final_state = None
        last_state = None
        stream_input: dict[str, Any] = {"recovery_count": recovery_count + 1}
        if reset_state:
            stream_input.update(reset_state)
        try:
            async for chunk in self._graph.astream(
                stream_input,
                config,
                stream_mode="values",
            ):
                last_state = chunk
                task_row = await self.get_task_state(task_id)
                if task_row and task_row.get("status") == "cancelled":
                    raise asyncio.CancelledError
            final_state = last_state
            final_status = _status_str(final_state.get("status", "completed"))
            await self._update_task_pg(
                task_id,
                final_status,
                _status_str(final_state.get("phase", "report")),
            )
            logger.info(
                "[%s] Recovery finished with task status '%s'",
                task_id[:8], final_status,
            )
        except asyncio.CancelledError:
            await self._update_task_pg(
                task_id,
                "cancelled",
                str(last_state.get("phase", "")) if last_state else "",
            )
            logger.info(f"[{task_id[:8]}] Recovery cancelled")
            raise
        except Exception as e:
            await self._update_task_pg(
                task_id, "failed",
                str(last_state.get("phase", "")) if last_state else "",
            )
            logger.error(f"[{task_id[:8]}] Recovery failed: {e}")
            try:
                from wren_agent.api.websocket import broadcast_to_task
                await broadcast_to_task(task_id, "task_status_change", {
                    "old": "running", "new": "failed",
                })
                await broadcast_to_task(task_id, "error", {
                    "message": f"Recovery failed: {str(e)[:300]}",
                })
            except Exception:
                pass
        finally:
            persist_state = final_state if final_state is not None else last_state
            if persist_state:
                try:
                    from wren_agent.tools.pg_store import persist_task_artifacts
                    await persist_task_artifacts(persist_state)
                except Exception as e:
                    logger.warning(
                        f"[{task_id[:8]}] Artifact persist failed: {e}"
                    )

    # ── Human-in-the-loop resume ──────────────────────────────────

    async def resume_with_answer(
        self, task_id: str, cid: str, answer: str, *, skip: bool = False
    ):
        """Answer a clarification and resume from pause.

        PG is the source of truth (migration 010): the decision is written to
        task_pauses so ANY engine instance — including one that never held
        the paused task — can resume it. The in-process asyncio.Event is only
        a fast wakeup for the instance that owns the task thread.
        """
        from wren_agent.tools.pg_store import resume_task

        status = "skipped" if skip else "answered"
        persisted_answer = None if skip else answer
        await persist_clarification_answer(
            task_id, cid, persisted_answer, status=status
        )
        payload = {"cid": cid, "answer": answer, "skip": skip}
        await resume_task(task_id, payload)
        ev = _task_pauses.get(task_id)
        if ev:
            setattr(ev, 'last_answer', payload)
            ev.set()
            logger.info(
                f"[{task_id[:8]}] Resumed after clarification "
                f"{'skip' if skip else 'answer'} (in-process)"
            )
        else:
            logger.info(
                f"[{task_id[:8]}] Clarification "
                f"{'skip' if skip else 'answer'} recorded via PG (cross-engine)"
            )
        return True

    async def resume_with_approval(self, task_id: str, aid: str, approved: bool, comment: str = ""):
        from wren_agent.tools.pg_store import resume_task

        await self._persist_approval_response(task_id, aid, approved, comment)
        await resume_task(task_id, {"aid": aid, "approved": approved, "comment": comment})
        ev = _task_pauses.get(task_id)
        if ev:
            setattr(ev, 'last_approval', {'aid': aid, 'approved': approved, 'comment': comment})
            ev.set()
            logger.info(f"[{task_id[:8]}] Resumed after approval (in-process)")
        else:
            logger.info(f"[{task_id[:8]}] Approval recorded via PG (cross-engine)")
        return True

    async def resume_with_bulk_approval(self, task_id: str, comment: str = ""):
        """Approve every pending item and resume the paused human_approval node once."""
        from wren_agent.tools.pg_store import resume_task

        await persist_bulk_approval_response(task_id, comment)
        payload = {"bulk": True, "approved": True, "comment": comment}
        await resume_task(task_id, payload)
        ev = _task_pauses.get(task_id)
        if ev:
            setattr(ev, "last_approval", payload)
            ev.set()
            logger.info(f"[{task_id[:8]}] Resumed after bulk approval (in-process)")
        else:
            logger.info(f"[{task_id[:8]}] Bulk approval recorded via PG (cross-engine)")
        return True

    async def _persist_clarification_answer(self, task_id: str, cid: str, answer: str):
        """Write clarification answer to PG task_clarifications table."""
        await persist_clarification_answer(task_id, cid, answer)

    async def _persist_approval_response(self, task_id: str, aid: str, approved: bool, comment: str):
        """Write approval response to PG task_approvals table."""
        await persist_approval_response(task_id, aid, approved, comment)

    async def get_task_state(self, task_id: str) -> dict | None:
        from wren_agent.tools.pg_store import get_pg_pool
        try:
            pool = get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM modeling_tasks WHERE task_id=$1", task_id)
                if not row: return None
                row_d = dict(row)
                return {"task_id": row_d["task_id"], "status": row_d["status"],
                        "phase": row_d["phase"], "modeling_goal": row_d["modeling_goal"],
                        "mode": row_d.get("mode", "manual"),
                        "created_at": str(row_d["created_at"]), "updated_at": str(row_d["updated_at"])}
        except Exception as e:
            logger.error(f"get_task_state failed: {e}")
            return None

    async def get_checkpoint_state(self, task_id: str) -> dict[str, Any]:
        """Return the latest checkpoint channel values for API observability."""
        if self._checkpointer is None:
            return {}
        try:
            checkpoint = await self._checkpointer.aget_tuple({
                "configurable": {"thread_id": task_id}
            })
            if checkpoint and checkpoint.checkpoint:
                return dict(checkpoint.checkpoint.get("channel_values", {}))
        except Exception:
            logger.debug("Checkpoint state read failed for %s", task_id, exc_info=True)
        return {}

    async def cancel_task(self, task_id: str) -> bool:
        task = self._active_tasks.get(task_id)
        ev = _task_pauses.pop(task_id, None)
        if ev:
            ev.set()

        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            task_state = await self.get_task_state(task_id)
            if not task_state:
                return False
            status = str(task_state.get("status", ""))
            if status == "cancelled":
                return True
            if status not in LIVE_TASK_STATUSES:
                return False

        await self._update_task_pg(task_id, "cancelled", "")
        try:
            from wren_agent.api.websocket import broadcast_to_task
            await broadcast_to_task(task_id, "task_status_change", {
                "old": "running", "new": "cancelled",
            })
        except Exception:
            logger.debug("Cancellation broadcast failed", exc_info=True)
        return True

    async def _update_task_pg(self, task_id: str, status: str, phase: str,
                              mode: str | None = None):
        from wren_agent.tools.pg_store import get_pg_pool
        try:
            pool = get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO modeling_tasks (task_id, thread_id, status, phase, modeling_goal, mode) "
                    "VALUES ($1, $2, $3, $4, $5, COALESCE($6, 'manual')) "
                    "ON CONFLICT (task_id) DO UPDATE SET status=$3, phase=$4, "
                    "mode=COALESCE($6, modeling_tasks.mode), updated_at=NOW()",
                    task_id, task_id, status, phase, "", mode,
                )
        except Exception as e:
            logger.debug(f"PG write skipped: {e}")


_runner: AgentRunner | None = None


def get_runner() -> AgentRunner:
    assert _runner is not None, "AgentRunner not initialized"
    return _runner


async def update_task_pg_status(task_id: str, status: str, phase: str = ""):
    """Public helper for nodes to update task status in PG.

    Called from HITL nodes (wait_human, human_approval) when pausing.
    """
    from wren_agent.tools.pg_store import get_pg_pool
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO modeling_tasks (task_id, thread_id, status, phase, modeling_goal) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (task_id) DO UPDATE SET status=$3, phase=$4, updated_at=NOW()",
                task_id, task_id, status, phase or status, "",
            )
    except Exception as e:
        logger.debug(f"PG write from node failed: {e}")


async def persist_clarification_answer(
    task_id: str, cid: str, answer: str | None, status: str = "answered"
):
    """Write a clarification resolution to PG task_clarifications.

    Used by the resume API (human answer) and by auto mode nodes
    (status='answered' with the recommended answer, or 'skipped' with None).
    """
    from wren_agent.tools.pg_store import get_pg_pool
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_clarifications SET status=$1, answer=$2, "
                "answered_at=NOW() WHERE id=$3 AND task_id=$4",
                status, answer, cid, task_id,
            )
    except Exception as e:
        logger.debug(f"Clarification answer persist skipped: {e}")


async def persist_approval_response(task_id: str, aid: str, approved: bool, comment: str):
    """Write an approval resolution to PG task_approvals.

    Used by the resume API (human decision) and by auto mode nodes.
    """
    from wren_agent.tools.pg_store import get_pg_pool
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_approvals SET status=$1, comment=$2, "
                "resolved_at=NOW() WHERE id=$3 AND task_id=$4",
                "approved" if approved else "rejected", comment, aid, task_id,
            )
    except Exception as e:
        logger.debug(f"Approval response persist skipped: {e}")


async def persist_bulk_approval_response(task_id: str, comment: str) -> None:
    """Mark every pending approval for a task as approved."""
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE task_approvals SET status=$1, comment=$2, "
            "resolved_at=NOW() WHERE task_id=$3 AND status='pending'",
            "approved", comment, task_id,
        )


async def init_runner():
    global _runner
    _runner = AgentRunner()
    await _runner.startup()


async def shutdown_runner():
    global _runner
    if _runner:
        await _runner.shutdown()
