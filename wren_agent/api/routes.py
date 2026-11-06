"""FastAPI REST routes for the Wren Modeling Agent — real implementations."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from wren_agent.models.task import SourceDatabase
from wren_agent.state import create_initial_state

router = APIRouter(prefix="/api/v1")


class CreateTaskRequest(BaseModel):
    modeling_goal: str = Field(..., max_length=50000, description="建模目的，最多50000字符")
    source_databases: list[dict]
    dw_target: dict
    mode: Literal["manual", "auto"] = "manual"  # auto = unattended (AI answers/approves)
    source_mode: Literal["business_db", "warehouse"] = "business_db"
    business_task_type: Literal["full", "ods_migration", "core_model"] = "full"
    # warehouse = explore existing dim/dwd/dws layers and produce DWS/ADS only
    # warehouse mode only: where NEW DWS/ADS tables land, e.g. {"dws": "db_x", "ads": "db_y"}
    landing: Optional[dict] = None
    warehouse_source_id: str = ""
    create_missing_databases: bool = False

    @model_validator(mode="after")
    def validate_business_landing(self) -> "CreateTaskRequest":
        if self.source_mode != "business_db":
            if self.business_task_type != "full":
                raise ValueError("business_task_type is only valid for business_db mode")
            return self
        if not self.warehouse_source_id:
            raise ValueError("business_db mode requires warehouse_source_id")
        landing = self.landing or {}
        required = (("ods",) if self.business_task_type == "ods_migration"
                    else ("ods", "dim", "dwd"))
        missing = [layer for layer in required if not landing.get(layer)]
        if missing:
            raise ValueError(f"business_db mode requires landing databases: {', '.join(missing)}")
        import re
        invalid = [landing[layer] for layer in required
                   if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(landing[layer]))]
        if invalid:
            raise ValueError("landing database names must be valid Doris identifiers")
        return self


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str


class ClarificationResponse(BaseModel):
    clarification_id: str
    answer: str = ""
    skip: bool = False


class ApprovalResponse(BaseModel):
    approval_id: str
    approved: bool
    comment: str = ""


class BulkApprovalRequest(BaseModel):
    comment: str = ""


# ── Routes ──────────────────────────────────────────────────────


def _parse_jsonb_list(val: Any) -> list:
    """asyncpg returns jsonb columns as str (no codec registered) — parse to list."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _failure_kind(state: dict[str, Any], status: str = "") -> str:
    """Classify a terminal failure for UI/recovery without a schema migration."""
    if status != "failed" and str(state.get("status", "")) not in (
        "failed", "TaskStatus.FAILED"
    ):
        return ""
    if state.get("failure_kind") == "validation" or state.get("validation_blocking"):
        return "validation"
    errors = state.get("errors", []) or []
    if any(
        "Critical design invariants failed" in str(message)
        or "no SQL artifacts were generated" in str(message)
        or "Missing design plan" in str(message)
        for message in errors
    ):
        return "validation"
    return "crash"


@router.get("/tasks")
async def list_tasks(status: str = ""):
    """List all modeling tasks, optionally filtered by status."""
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM modeling_tasks WHERE status=$1 ORDER BY created_at DESC LIMIT 50",
                status,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM modeling_tasks ORDER BY created_at DESC LIMIT 50"
            )
        return {"tasks": [dict(r) for r in rows], "count": len(rows)}


def _build_source_databases(source_databases: list[dict]) -> list[SourceDatabase]:
    """Build SourceDatabase models, preserving the registry `id` when provided.

    Dropping the client-sent id would generate a random uuid4 (model default),
    breaking the credential lookup against source_db_registry.
    """
    source_dbs = []
    for db in source_databases:
        kwargs: dict[str, Any] = dict(
            name=db.get("name", "unknown"),
            db_type=db.get("db_type", "mysql"),
            host=db.get("host", ""),
            port=db.get("port", 3306),
            database=db.get("database", ""),
            mcp_server_name=db.get("mcp_server_name", f"mcp_{db.get('name','db')}"),
            connection_params=db.get("connection_params") or {},
        )
        if db.get("id"):
            kwargs["id"] = str(db["id"])
        # Warehouse mode: layer -> database mapping rides inside
        # connection_params.layers (see wayfinder T-01/T-10)
        layers = (db.get("connection_params") or {}).get("layers")
        if isinstance(layers, dict):
            kwargs["layers"] = layers
        source_dbs.append(SourceDatabase(**kwargs))
    return source_dbs


@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(request: CreateTaskRequest):
    """Create a new modeling task and launch the agent in background."""
    from wren_agent.engine import get_runner

    task_id = str(uuid.uuid4())

    if request.source_mode == "business_db":
        from wren_agent.tools.pg_store import get_source_db
        warehouse = await get_source_db(request.warehouse_source_id)
        if (not warehouse or str(warehouse.get("db_type", "")).lower() != "doris"
                or warehouse.get("status") != "active"):
            raise HTTPException(400, "warehouse_source_id must reference an active Doris source")

    # Build source databases (keeps registry id for credential lookup)
    source_dbs = _build_source_databases(request.source_databases)

    # Warehouse landing choice (库级落库选择): ride on the first source's
    # connection_params so deployment's _warehouse_layers() sees it
    if request.landing and source_dbs:
        source_dbs[0].connection_params = {
            **(source_dbs[0].connection_params or {}),
            "landing": request.landing,
        }

    state = create_initial_state(
        task_id=task_id,
        modeling_goal=request.modeling_goal,
        source_databases=source_dbs,
        dw_target=request.dw_target,
        mode=request.mode,
        source_mode=request.source_mode,
        business_task_type=request.business_task_type,
        warehouse_source_id=request.warehouse_source_id,
        landing=request.landing,
        create_missing_databases=request.create_missing_databases,
    )

    runner = get_runner()
    runner.run_task_background(state)

    return CreateTaskResponse(task_id=task_id, status="pending")


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get the current status of a modeling task."""
    from wren_agent.engine import get_runner

    runner = get_runner()
    state = await runner.get_task_state(task_id)

    if not state:
        raise HTTPException(404, f"Task '{task_id}' not found")

    checkpoint_state = await runner.get_checkpoint_state(task_id)
    merged_state = {**checkpoint_state, **state}

    return {
        "task_id": state["task_id"],
        "status": state["status"],
        "phase": state.get("phase", ""),
        "modeling_goal": state.get("modeling_goal", ""),
        "mode": state.get("mode", "manual"),
        "business_task_type": state.get("business_task_type", "full"),
        "pending_human_input": state.get("pending_human_input", False),
        "human_input_type": state.get("human_input_type", ""),
        "errors_count": len(checkpoint_state.get("errors", [])),
        "warnings_count": len(checkpoint_state.get("warnings", [])),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "recovery_count": checkpoint_state.get("recovery_count", 0),
        "failure_kind": _failure_kind(merged_state, str(state.get("status", ""))),
    }


@router.get("/tasks/{task_id}/stages")
async def get_task_stages(task_id: str):
    """Return stage lifecycle, latest artifact versions and ODS table checks."""
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM modeling_tasks WHERE task_id=$1", task_id)
        if not exists:
            raise HTTPException(404, f"Task '{task_id}' not found")
        stages = [dict(row) for row in await conn.fetch(
            "SELECT * FROM modeling_stages WHERE task_id=$1 ORDER BY "
            "CASE stage WHEN 'exploration' THEN 1 WHEN 'ods' THEN 2 ELSE 3 END", task_id)]
        artifacts = [dict(row) for row in await conn.fetch(
            "SELECT DISTINCT ON (stage, artifact_type) stage, artifact_type, version, "
            "schema_fingerprint, created_at FROM stage_artifacts WHERE task_id=$1 "
            "ORDER BY stage, artifact_type, version DESC", task_id)]
        ods = [dict(row) for row in await conn.fetch(
            "SELECT DISTINCT ON (manifest_version, table_identity) * FROM ods_table_runs "
            "WHERE task_id=$1 ORDER BY manifest_version, table_identity, attempt DESC", task_id)]
    return {"task_id": task_id, "stages": stages, "artifacts": artifacts, "ods_tables": ods}


@router.get("/tasks/{task_id}/lineage")
async def get_task_lineage(task_id: str):
    """Return published core assets and their warehouse/DAG associations."""
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM core_asset_lineage WHERE task_id=$1 "
            "ORDER BY core_model_version DESC, layer, table_name, asset_kind", task_id)
    return {"task_id": task_id, "assets": [dict(row) for row in rows]}


@router.get("/modeling/stage-metrics")
async def get_stage_metrics(window: int = 100):
    """Stage success rates for the operations dashboard (latest run per task/stage)."""
    from wren_agent.tools.pg_store import get_pg_pool
    window = max(1, min(window, 1000))
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "WITH recent_tasks AS (SELECT DISTINCT task_id, max(updated_at) AS updated_at "
            "FROM modeling_stages GROUP BY task_id ORDER BY updated_at DESC LIMIT $1), "
            "latest AS (SELECT DISTINCT ON (s.task_id, s.stage) s.stage, s.status "
            "FROM modeling_stages s JOIN recent_tasks r USING (task_id) "
            "ORDER BY s.task_id, s.stage, s.attempt DESC) "
            "SELECT stage, count(*)::int AS total, "
            "count(*) FILTER (WHERE status='succeeded')::int AS succeeded, "
            "count(*) FILTER (WHERE status='partial_success')::int AS partial_success "
            "FROM latest GROUP BY stage ORDER BY stage", window)
    return {"window": window, "stages": [dict(row) for row in rows]}


@router.post("/tasks/{task_id}/stages/{stage}/retry")
async def retry_task_stage(task_id: str, stage: str, table_identity: str = ""):
    """Retry one failed stage; ODS retries may be narrowed to one table."""
    if stage not in {"exploration", "ods", "core_model"}:
        raise HTTPException(400, "unsupported stage")
    if stage != "ods":
        raise HTTPException(409, "local retry is currently supported for ODS tables only")
    table_identity = table_identity.strip()
    if not table_identity:
        raise HTTPException(400, "ODS retry requires table_identity")
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"ods-retry:{task_id}:{table_identity}",
            )
            row = await conn.fetchrow(
                "SELECT * FROM modeling_stages WHERE task_id=$1 AND stage=$2", task_id, stage)
            if not row:
                raise HTTPException(404, "stage not found")
            if row["status"] not in {"failed", "partial_success"}:
                raise HTTPException(409, "only failed or partial stages can be retried")
            latest = await conn.fetchrow(
                "SELECT * FROM ods_table_runs WHERE task_id=$1 AND table_identity=$2 "
                "ORDER BY attempt DESC LIMIT 1", task_id, table_identity)
            if not latest:
                raise HTTPException(404, "ODS table run not found")
            if latest["status"] != "failed":
                raise HTTPException(409, "only failed ODS tables can be retried")
            if latest["error_type"] != "transient" or latest["attempt"] >= 3:
                raise HTTPException(409, "ODS table is not retryable")
            manifest_version = latest["manifest_version"]
            attempt = latest["attempt"] + 1
            manifest_payload = await conn.fetchval(
                "SELECT payload FROM stage_artifacts WHERE task_id=$1 AND stage='ods' "
                "AND artifact_type='ods_manifest' AND version=$2", task_id, manifest_version)
            manifest = (manifest_payload if isinstance(manifest_payload, dict)
                        else json.loads(manifest_payload or "{}"))
            target = next((item.get("ods_table") for item in manifest.get("tables", [])
                           if item.get("table_identity") == table_identity), None)
            if not isinstance(target, str) or not target.strip():
                raise HTTPException(409, "ODS table is missing from the deployment manifest")
            target = target.strip()
            dag = await conn.fetchrow(
                "SELECT project_code, workflow_code FROM dag_table_map "
                "WHERE table_name=$1 OR table_name LIKE $2 LIMIT 1", target, f"%.{target}")
            if not dag:
                raise HTTPException(409, "ODS table has no deployed DAG mapping")
            await conn.execute(
                "INSERT INTO ods_table_runs (task_id, manifest_version, table_identity, attempt, "
                "required, sync_mode, status) VALUES ($1,$2,$3,$4,$5,$6,'running')",
                task_id, manifest_version, table_identity, attempt,
                latest["required"], latest["sync_mode"])
            await conn.execute(
                "UPDATE modeling_stages SET status='running', attempt=attempt+1, updated_at=NOW() "
                "WHERE task_id=$1 AND stage='ods'", task_id)

    # A stage retry is a fresh scheduler request. The existing ODS DAG mapping
    # is authoritative, so no new workflow is created by the API.
    from wren_agent.tools.ds_api import get_ds_api
    ds = get_ds_api()
    try:
        instance_id = await ds.start_workflow(int(dag["project_code"]), int(dag["workflow_code"]))
        if instance_id is None:
            raise RuntimeError("DolphinScheduler did not create a workflow instance")
    except Exception as exc:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ods_table_runs SET status='failed', error_type='transient', error=$5, "
                "finished_at=NOW(), updated_at=NOW() WHERE task_id=$1 AND manifest_version=$2 "
                "AND table_identity=$3 AND attempt=$4", task_id, manifest_version,
                table_identity, attempt, str(exc)[:1000])
            await conn.execute(
                "UPDATE modeling_stages SET status='failed', error=$2, updated_at=NOW() "
                "WHERE task_id=$1 AND stage='ods'", task_id, str(exc)[:1000])
        raise HTTPException(502, f"failed to start ODS retry: {exc}") from exc
    finally:
        ds.close()
    return {"status": "queued", "stage": stage, "table_identity": table_identity,
            "attempt": attempt, "instance_id": instance_id}


@router.get("/tasks/{task_id}/clarifications")
async def get_clarifications(task_id: str):
    """Get pending clarifications from PG tables + checkpointer fallback."""
    from wren_agent.config import settings as s

    items = []

    # 1. Try PG table first (primary source)
    try:
        from wren_agent.tools.pg_store import get_pg_pool
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, question, context, candidates, impact_level, "
                "blocking, status, clarification_type, answer, "
                "recommended, recommended_reason "
                "FROM task_clarifications WHERE task_id=$1 ORDER BY created_at",
                task_id,
            )
            for row in rows:
                items.append({
                    "id": str(row["id"]),
                    "question": row["question"],
                    "context": row.get("context", ""),
                    "candidates": _parse_jsonb_list(row.get("candidates")),
                    "recommended": row.get("recommended"),
                    "recommended_reason": row.get("recommended_reason", "") or "",
                    "impact_level": row.get("impact_level", "medium"),
                    "blocking": row["blocking"],
                    "status": str(row["status"]),
                    "answer": row.get("answer"),
                    "clarification_type": row.get("clarification_type", ""),
                })
    except Exception:
        pass

    # 2. Fallback: LangGraph checkpointer
    if not items:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            with PostgresSaver.from_conn_string(s.pg.dsn) as checkpointer:
                await checkpointer.setup()
                snapshot = await asyncio.get_running_loop().run_in_executor(
                None, checkpointer.get_tuple,{"configurable": {"thread_id": task_id}})

                if snapshot and snapshot.checkpoint:
                    channel_values = snapshot.checkpoint.get("channel_values", {})
                    clarifications = channel_values.get("clarifications", [])
                    for c in clarifications:
                        if isinstance(c, dict):
                            items.append({
                                "id": c.get("id", ""),
                                "question": c.get("question", ""),
                                "context": c.get("context", ""),
                                "candidates": c.get("candidates", []),
                                "recommended": c.get("recommended"),
                                "recommended_reason": c.get("recommended_reason", ""),
                                "blocking": c.get("blocking", True),
                                "impact_level": c.get("impact_level", "medium"),
                                "clarification_type": c.get("clarification_type", ""),
                                "status": str(c.get("status", "pending")),
                                "answer": c.get("answer"),
                            })
        except Exception:
            pass

    return items


@router.post("/tasks/{task_id}/clarifications")
async def answer_clarification(task_id: str, response: ClarificationResponse):
    """Answer a clarification and resume the agent."""
    from wren_agent.engine import get_runner

    answer = response.answer.strip()
    if not response.skip and not answer:
        raise HTTPException(422, "Answer must not be empty")

    runner = get_runner()
    try:
        await runner.resume_with_answer(
            task_id,
            response.clarification_id,
            answer,
            skip=response.skip,
        )
        return {
            "status": "skipped" if response.skip else "resumed",
            "task_id": task_id,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to resume: {e}")


@router.get("/tasks/{task_id}/approvals")
async def get_approvals(task_id: str):
    """Get pending approvals from PG tables + checkpointer fallback."""
    from wren_agent.config import settings as s

    items = []

    # 1. Try PG table first
    try:
        from wren_agent.tools.pg_store import get_pg_pool
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, description, artifact_type, status, comment "
                "FROM task_approvals WHERE task_id=$1 ORDER BY created_at",
                task_id,
            )
            for row in rows:
                items.append({
                    "id": str(row["id"]),
                    "title": row["title"],
                    "description": row.get("description", ""),
                    "artifact_type": row.get("artifact_type", ""),
                    "status": str(row["status"]),
                })
    except Exception:
        pass

    # 2. Fallback: LangGraph checkpointer
    if not items:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            with PostgresSaver.from_conn_string(s.pg.dsn) as checkpointer:
                await checkpointer.setup()
                snapshot = await asyncio.get_running_loop().run_in_executor(
                None, checkpointer.get_tuple,{"configurable": {"thread_id": task_id}})
                if snapshot and snapshot.checkpoint:
                    channel_values = snapshot.checkpoint.get("channel_values", {})
                    approvals = channel_values.get("approval_items", [])
                    for a in approvals:
                        if isinstance(a, dict):
                            items.append({
                                "id": a.get("id", ""),
                                "title": a.get("title", ""),
                                "description": a.get("description", ""),
                                "artifact_type": a.get("artifact_type", ""),
                                "status": str(a.get("status", "pending")),
                            })
        except Exception:
            pass

    return items


@router.post("/tasks/{task_id}/approvals")
async def respond_approval(task_id: str, response: ApprovalResponse):
    """Approve or reject and resume the agent."""
    from wren_agent.engine import get_runner

    runner = get_runner()
    try:
        await runner.resume_with_approval(
            task_id,
            response.approval_id,
            response.approved,
            response.comment,
        )
        # Notify all watchers so every open client refreshes its pending
        # approval count/list (manual mode previously emitted nothing).
        try:
            from wren_agent.api.websocket import broadcast_to_task
            await broadcast_to_task(task_id, "approval_resolved", {
                "id": response.approval_id,
                "approved": response.approved,
                "auto": False,
            })
        except Exception:
            pass  # Fire-and-forget — resume already succeeded
        return {"status": "resumed", "task_id": task_id}
    except Exception as e:
        raise HTTPException(500, f"Failed to resume: {e}")


@router.post("/tasks/{task_id}/approvals/bulk")
async def respond_bulk_approval(task_id: str, body: BulkApprovalRequest = BulkApprovalRequest()):
    """Approve every pending item in one resume (one-click approval)."""
    from wren_agent.engine import get_runner

    runner = get_runner()
    try:
        await runner.resume_with_bulk_approval(task_id, body.comment)
        try:
            from wren_agent.api.websocket import broadcast_to_task
            await broadcast_to_task(task_id, "approval_resolved", {
                "bulk": True,
                "approved": True,
                "auto": False,
            })
        except Exception:
            pass
        return {"status": "resumed", "task_id": task_id}
    except Exception as e:
        raise HTTPException(500, f"Failed to bulk-approve: {e}")


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    from wren_agent.engine import get_runner

    runner = get_runner()
    cancelled = await runner.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(404, f"Task '{task_id}' not running")
    return {"status": "cancelled", "task_id": task_id}


@router.post("/tasks/{task_id}/recover")
async def recover_task(task_id: str):
    """Recover a crashed task from the last LangGraph checkpoint.

    The task is resumed from the last successful superstep checkpoint.
    Recovery is manual (API-triggered) and capped to prevent retry loops.
    Deterministic design-validation failures are rejected because replaying
    their terminal checkpoint cannot repair the invalid design.

    Only tasks in 'failed' or 'cancelled' status can be recovered.
    Running tasks are rejected (already running).
    Completed tasks are rejected (nothing to recover).
    """
    from wren_agent.engine import get_runner

    runner = get_runner()
    result = await runner.recover_task(task_id)

    status_map = {
        "not_found": 404,
        "already_running": 409,
        "completed": 409,
        "no_checkpointer": 500,
        "no_checkpoint": 422,
        "not_recoverable": 422,
        "validation_failed": 422,
        "recovery_limit_reached": 429,
    }
    status_code = status_map.get(result["status"], 200)
    if status_code not in (200,):
        raise HTTPException(status_code, result["message"])
    return result


class CleanupResponse(BaseModel):
    deleted_count: int = 0
    deleted_task_ids: list[str] = []
    details: dict[str, int] = {}  # table -> rows deleted
    skipped_active: list[str] = []  # tasks skipped because not failed


@router.delete("/tasks/cleanup")
async def cleanup_failed_tasks(task_id: str = ""):
    """Delete all failed tasks and their associated data, or a specific failed task.

    - Without ``task_id``: cleans ALL failed tasks.
    - With ``task_id``: cleans only that task (must be in 'failed' status).

    Deletes data from all related tables including LangGraph checkpointer tables
    and returns a summary of what was removed.
    """
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    deleted_ids: list[str] = []
    details: dict[str, int] = {}
    skipped_active: list[str] = []

    async with pool.acquire() as conn:
        # 1. Find target tasks — only 'failed' status
        if task_id:
            row = await conn.fetchrow(
                "SELECT task_id, status FROM modeling_tasks WHERE task_id = $1",
                task_id,
            )
            if not row:
                raise HTTPException(404, f"Task '{task_id}' not found")
            if row["status"] != "failed":
                raise HTTPException(
                    400,
                    f"Task '{task_id}' has status '{row['status']}', not 'failed'. "
                    "Only failed tasks can be cleaned up.",
                )
            target_ids = [task_id]
        else:
            rows = await conn.fetch(
                "SELECT task_id FROM modeling_tasks WHERE status = 'failed'"
            )
            target_ids = [r["task_id"] for r in rows]

        if not target_ids:
            return CleanupResponse(
                deleted_count=0,
                deleted_task_ids=[],
                details={},
                skipped_active=skipped_active,
            )

        logger = __import__("logging").getLogger(__name__)
        logger.info(f"Cleaning up {len(target_ids)} failed task(s): {target_ids}")

        # 2. Delete from tables WITHOUT FK cascade (must go first)
        for tbl in ("task_artifacts", "task_lifecycle", "llm_trace"):
            result = await conn.execute(
                f"DELETE FROM {tbl} WHERE task_id = ANY($1)", target_ids
            )
            count = int(result.split()[-1]) if result else 0
            if count:
                details[tbl] = count

        # 3. Delete from LangGraph checkpointer tables (if they exist)
        for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            try:
                result = await conn.execute(
                    f"DELETE FROM {tbl} WHERE thread_id = ANY($1)", target_ids
                )
                count = int(result.split()[-1]) if result else 0
                if count:
                    details[tbl] = count
            except Exception:
                # Table may not exist — LangGraph checkpointer not always active
                pass

        # 4. Delete from modeling_tasks — CASCADE handles the rest
        #    (task_audit_log, task_clarifications, task_approvals, schema_snapshots)
        result = await conn.execute(
            "DELETE FROM modeling_tasks WHERE task_id = ANY($1)", target_ids
        )
        count = int(result.split()[-1]) if result else 0
        details["modeling_tasks"] = count
        deleted_ids = target_ids

    logger.info(
        f"Cleanup complete: {len(deleted_ids)} tasks, details: {details}"
    )

    return CleanupResponse(
        deleted_count=len(deleted_ids),
        deleted_task_ids=deleted_ids,
        details=details,
        skipped_active=skipped_active,
    )


@router.get("/tasks/{task_id}/llm-traces")
async def get_llm_traces(
    task_id: str,
    node: str = "",
    limit: int = 50,
    offset: int = 0,
    full: bool = False,
):
    """Get persisted LLM conversation traces for a task.

    Prompt/response corpus per LLM call for skill optimization and eval grading.
    Full-corpus export remains disabled by policy even for authenticated clients.
    """
    if full:
        raise HTTPException(
            403,
            "Full LLM traces are disabled by security policy",
        )

    from wren_agent.tools.pg_store import get_pg_pool
    from wren_agent.tools.redaction import sanitize_for_output

    limit = max(1, min(limit, 200))
    cap = 2000

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if node:
            rows = await conn.fetch(
                "SELECT * FROM llm_trace WHERE task_id=$1 AND node=$2 "
                "ORDER BY created_at LIMIT $3 OFFSET $4",
                task_id, node, limit, offset,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM llm_trace WHERE task_id=$1 "
                "ORDER BY created_at LIMIT $2 OFFSET $3",
                task_id, limit, offset,
            )

    traces = []
    for row in rows:
        d = dict(row)
        d["skills"] = _parse_jsonb_list(d.get("skills"))
        for field in ("system_prompt", "user_message", "response"):
            val = d.get(field) or ""
            if cap and len(val) > cap:
                d[field] = val[:cap] + f"... [truncated, {len(val)} chars total]"
        traces.append(sanitize_for_output(d))

    return {"task_id": task_id, "count": len(traces), "traces": traces}


@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str, phase: str = "", limit: int = 100, offset: int = 0):
    """Get audit logs from PG."""
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if phase:
            rows = await conn.fetch(
                "SELECT * FROM task_audit_log WHERE task_id=$1 AND phase=$2 ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                task_id, phase, limit, offset,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM task_audit_log WHERE task_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                task_id, limit, offset,
            )
        from wren_agent.tools.redaction import sanitize_for_output
        return sanitize_for_output([dict(r) for r in rows])


async def _load_design_plan(task_id: str) -> dict | None:
    """Load the latest design plan — checkpointer first, then task_artifacts.

    Shared by get_design_plan and the approval-narratives endpoint.
    """
    from wren_agent.config import settings as s
    from langgraph.checkpoint.postgres import PostgresSaver

    try:
        with PostgresSaver.from_conn_string(s.pg.dsn) as checkpointer:
            checkpointer.setup()
            snapshot = await asyncio.get_running_loop().run_in_executor(
                None, checkpointer.get_tuple,{"configurable": {"thread_id": task_id}})

            if snapshot and snapshot.checkpoint:
                channel_values = snapshot.checkpoint.get("channel_values", {})
                plan = channel_values.get("design_plan")
                if plan:
                    # Checkpointer returns the pydantic model — normalize to a
                    # plain dict so callers can .get() on it (regression: the
                    # narratives endpoint crashed with AttributeError on models).
                    return plan if isinstance(plan, dict) else plan.model_dump(mode="json")
    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.warning(f"Checkpointer read failed for {task_id}: {e}")

    # Fallback: persisted artifacts (written incrementally by the node tracer)
    from wren_agent.tools.pg_store import load_task_artifacts
    persisted = await load_task_artifacts(task_id)
    if persisted and persisted.get("design_plan"):
        return persisted["design_plan"]
    return None


@router.get("/tasks/{task_id}/design-plan")
async def get_design_plan(task_id: str):
    """Get the design plan — checkpointer first, then persisted task_artifacts.

    The graph may run without a checkpointer (bare compile); in that case the
    node tracer persists design_plan incrementally to task_artifacts, which
    also serves running tasks.
    """
    plan = await _load_design_plan(task_id)
    if plan is None:
        raise HTTPException(404, "Design plan not available yet")
    return plan


@router.get("/tasks/{task_id}/approvals/{approval_id}/narratives")
async def get_approval_narratives(task_id: str, approval_id: str):
    """Narrative blocks + metric registry status for one approval round.

    Returns the three LLM blocks (goal_interpretation / metric_interpretation
    / expected_impact) plus the computed risk block from approval_narratives
    (migration 012). Missing blocks default to status=pending so the frontend
    can poll/degrade; metric_status is matched against the latest design plan
    at read time (history is approximate by design, see wayfinder T-02).
    """
    from wren_agent.tools.pg_store import (
        get_pg_pool, load_approval_narratives, query_metric_registry,
    )

    # Ownership check: the approval must belong to this task.
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT 1 FROM task_approvals WHERE id = $1 AND task_id = $2",
                approval_id, task_id,
            )
        if not exists:
            raise HTTPException(404, "Approval not found for this task")
    except HTTPException:
        raise
    except Exception:
        pass  # PG hiccup — proceed; missing narratives will surface as pending

    rows = await load_approval_narratives(approval_id)
    by_block = {r["block"]: r for r in rows}

    blocks = []
    for block in ("goal_interpretation", "metric_interpretation", "expected_impact", "risk"):
        row = by_block.get(block)
        blocks.append({
            "block": block,
            "status": (row or {}).get("status", "pending"),
            "content": (row or {}).get("content_json"),
            "error": (row or {}).get("error", ""),
            "attempts": (row or {}).get("attempts", 0),
        })

    # Metric registry status vs the latest design plan (name match, id wins)
    metric_status: list[dict] = []
    plan = await _load_design_plan(task_id)
    if plan:
        # Defensive: never assume dict (checkpointer / mocks may hand a model)
        if not isinstance(plan, dict):
            plan = plan.model_dump(mode="json")
        registry = await query_metric_registry()
        by_name = {
            str(r.get("metric_name", "")).lower(): r for r in registry
        }
        seen = set()
        for d in plan.get("dws_designs", []):
            for m in d.get("measures", []):
                name = m.get("name", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                reg = by_name.get(name.lower())
                metric_status.append({
                    "name": name, "layer": "DWS",
                    "registered": bool(reg),
                    "registry_id": (reg or {}).get("metric_id") if reg else None,
                    "active": bool(reg and reg.get("active", True)),
                })
        for d in plan.get("ads_designs", []):
            for m in d.get("metrics", []):
                name = m.get("name", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                reg = by_name.get(name.lower())
                metric_status.append({
                    "name": name, "layer": "ADS",
                    "registered": bool(reg or m.get("metric_registry_id")),
                    "registry_id": (reg or {}).get("metric_id") or m.get("metric_registry_id"),
                    "active": bool(reg and reg.get("active", True)),
                })

    return {"approval_id": approval_id, "task_id": task_id, "blocks": blocks,
            "metric_status": metric_status}


@router.get("/tasks/{task_id}/report")
async def get_construction_report(task_id: str):
    """Get both technical and business-friendly construction reports."""
    from wren_agent.config import settings as s
    from wren_agent.nodes.reporting import build_plain_report
    from langgraph.checkpoint.postgres import PostgresSaver

    try:
        with PostgresSaver.from_conn_string(s.pg.dsn) as checkpointer:
            checkpointer.setup()
            snapshot = await asyncio.get_running_loop().run_in_executor(
                None, checkpointer.get_tuple,{"configurable": {"thread_id": task_id}})

            if snapshot and snapshot.checkpoint:
                channel_values = snapshot.checkpoint.get("channel_values", {})
                report = channel_values.get("construction_report")
                if report:
                    return {
                        "report": report,
                        "plain_report": channel_values.get("plain_report")
                        or build_plain_report(channel_values),
                        "task_id": task_id,
                    }
    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.warning(f"Checkpointer read failed for {task_id}: {e}")

    # Fallback: persisted artifacts (written incrementally by the node tracer)
    from wren_agent.tools.pg_store import load_task_artifacts
    persisted = await load_task_artifacts(task_id)
    if persisted and persisted.get("construction_report"):
        from wren_agent.engine import get_runner
        task_state = await get_runner().get_task_state(task_id) or {}
        report_state = {**persisted, **task_state}
        return {
            "report": persisted["construction_report"],
            "plain_report": persisted.get("plain_report")
            or build_plain_report(report_state),
            "task_id": task_id,
        }

    # Failed tasks predating the failure-report routes have no stored report —
    # build one on the fly from their persisted errors/validation/timeline so
    # the dashboard report page never shows a bare 404 for a finished task.
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT status, phase, modeling_goal, created_at FROM modeling_tasks WHERE task_id=$1",
            task_id,
        )
    if task_row and task_row["status"] == "failed":
        from wren_agent.nodes.reporting import build_failure_report
        fallback_state = dict(persisted or {})
        fallback_state.update({
            "task_id": task_id,
            "status": "failed",
            "phase": task_row["phase"],
            "modeling_goal": task_row["modeling_goal"],
            "created_at": task_row["created_at"],
        })
        return {
            "report": build_failure_report(fallback_state),
            "plain_report": build_plain_report(fallback_state),
            "task_id": task_id,
        }

    raise HTTPException(404, "Report not generated yet")


async def _load_runtime_pg_data(task_id: str) -> dict:
    """Read clarifications & approvals from PG for running tasks.

    Used as a fallback when the LangGraph checkpointer has no data
    (e.g. graph compiled without checkpointer, or snapshot empty).
    """
    data: dict[str, Any] = {
        "clarifications": [],
        "approval_items": [],
    }
    try:
        from wren_agent.tools.pg_store import get_pg_pool
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            # Clarifications
            rows = await conn.fetch(
                "SELECT id, question, context, candidates, impact_level, "
                "blocking, status, clarification_type, answer, "
                "recommended, recommended_reason "
                "FROM task_clarifications WHERE task_id=$1 ORDER BY created_at",
                task_id,
            )
            for row in rows:
                data["clarifications"].append({
                    "id": str(row["id"]),
                    "question": row["question"],
                    "context": row.get("context", ""),
                    "candidates": _parse_jsonb_list(row.get("candidates")),
                    "recommended": row.get("recommended"),
                    "recommended_reason": row.get("recommended_reason", "") or "",
                    "impact_level": row.get("impact_level", "medium"),
                    "blocking": row["blocking"],
                    "status": str(row["status"]),
                    "answer": row.get("answer"),
                    "clarification_type": row.get("clarification_type", ""),
                })

            # Approvals
            rows = await conn.fetch(
                "SELECT id, title, description, artifact_type, status, comment "
                "FROM task_approvals WHERE task_id=$1 ORDER BY created_at",
                task_id,
            )
            for row in rows:
                data["approval_items"].append({
                    "id": str(row["id"]),
                    "title": row["title"],
                    "description": row.get("description", ""),
                    "artifact_type": row.get("artifact_type", ""),
                    "status": str(row["status"]),
                })
    except Exception:
        pass
    return data


@router.get("/tasks/{task_id}/full-state")
async def get_task_full_state(task_id: str):
    """Get the complete lifecycle state for a task.

    For running tasks: reads from LangGraph checkpointer snapshot.
    For completed/failed tasks: reads from persisted PG tables.
    Returns all intermediate products, artifacts, and lifecycle data.
    """
    from wren_agent.config import settings as s
    from wren_agent.tools.pg_store import load_task_artifacts
    from wren_agent.tools.redaction import sanitize_for_output

    # 1. Get basic task info from modeling_tasks table
    from wren_agent.tools.pg_store import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT * FROM modeling_tasks WHERE task_id = $1", task_id
        )

    if not task_row:
        raise HTTPException(404, f"Task '{task_id}' not found")

    task_basic = dict(task_row)
    status = str(task_basic.get("status", ""))

    # Helper to serialize model objects
    def _serialize(val: Any) -> Any:
        if val is None:
            return None
        if hasattr(val, "model_dump"):
            return val.model_dump()
        if hasattr(val, "dict"):
            return val.dict()
        if isinstance(val, list):
            return [_serialize(v) for v in val]
        if isinstance(val, dict):
            return {k: _serialize(v) for k, v in val.items()}
        return val

    # 2. Check if completed → use persisted tables
    terminal_statuses = ("completed", "failed", "cancelled")
    if status in terminal_statuses:
        persisted = await load_task_artifacts(task_id)
        if persisted:
            # Recovery/failure metadata lives in the LangGraph checkpoint,
            # while artifacts live in dedicated tables. Merge only the small
            # metadata subset so existing tasks work without a DB migration.
            from wren_agent.engine import get_runner
            checkpoint_state = await get_runner().get_checkpoint_state(task_id)
            failure_state = {**persisted, **checkpoint_state}
            return sanitize_for_output({
                "task": task_basic,
                "from": "persisted",
                **persisted,
                "validation_blocking": bool(
                    checkpoint_state.get("validation_blocking")
                    or _failure_kind(failure_state, status) == "validation"
                ),
                "failure_kind": _failure_kind(failure_state, status),
                "recovery_count": checkpoint_state.get("recovery_count", 0),
            })

    # 3. Running task → read from LangGraph checkpointer
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(s.pg.dsn) as checkpointer:
            checkpointer.setup()
            snapshot = await asyncio.get_running_loop().run_in_executor(
                None, checkpointer.get_tuple,
                {"configurable": {"thread_id": task_id}}
            )

            if snapshot and snapshot.checkpoint:
                channel_values = snapshot.checkpoint.get("channel_values", {})

                return sanitize_for_output({
                    "task": task_basic,
                    "from": "checkpointer",
                    "design_plan": _serialize(channel_values.get("design_plan")),
                    "construction_report": channel_values.get("construction_report"),
                    "plain_report": channel_values.get("plain_report"),
                    "data_asset_map": channel_values.get("data_asset_map"),
                    "kg_export_id": channel_values.get("kg_export_id"),
                    "explorations": _serialize(channel_values.get("explorations", {})),
                    "sensitive_fields": _serialize(channel_values.get("sensitive_fields", [])),
                    "schema_drifts": _serialize(channel_values.get("schema_drifts", [])),
                    "incremental_strategies": _serialize(channel_values.get("incremental_strategies", [])),
                    "matched_blueprints": _serialize(channel_values.get("matched_blueprints", [])),
                    "validation_results": _serialize(channel_values.get("validation_results", [])),
                    "execution_results": _serialize(channel_values.get("execution_results", [])),
                    "error_classifications": _serialize(channel_values.get("error_classifications", [])),
                    "clarifications": _serialize(channel_values.get("clarifications", [])),
                    "approval_items": _serialize(channel_values.get("approval_items", [])),
                    "node_timeline": channel_values.get("node_timeline", []),
                    "audit_log": channel_values.get("audit_log", []),
                    "errors": channel_values.get("errors", []),
                    "warnings": channel_values.get("warnings", []),
                    "validation_blocking": channel_values.get("validation_blocking", False),
                    "failure_kind": _failure_kind(channel_values, status),
                    "recovery_count": channel_values.get("recovery_count", 0),
                    "current_node": channel_values.get("current_node", ""),
                    "pending_human_input": channel_values.get("pending_human_input", False),
                    "human_input_type": channel_values.get("human_input_type", ""),
                    "mode": channel_values.get("mode", "manual"),
                    "modeling_goal": channel_values.get("modeling_goal", ""),
                })

            # No checkpoint (bare graph) → read runtime data persisted
            # incrementally by the node tracer, plus PG clarif/approvals
            runtime = await load_task_artifacts(task_id) or {}
            pg_data = await _load_runtime_pg_data(task_id)
            return sanitize_for_output({
                "task": task_basic,
                "from": "runtime_pg",
                "design_plan": runtime.get("design_plan"),
                "construction_report": runtime.get("construction_report"),
                "plain_report": runtime.get("plain_report"),
                "data_asset_map": runtime.get("data_asset_map"),
                "kg_export_id": runtime.get("kg_export_id"),
                "validation_results": runtime.get("validation_results") or [],
                "execution_results": runtime.get("execution_results") or [],
                "error_classifications": runtime.get("error_classifications") or [],
                "node_timeline": runtime.get("node_timeline") or [],
                "audit_log": runtime.get("audit_log") or [],
                "errors": runtime.get("errors") or [],
                "warnings": runtime.get("warnings") or [],
                "modeling_goal": str(task_basic.get("modeling_goal", "") or ""),
                **pg_data,
            })

    except Exception as e:
        # Checkpointer unavailable → supplement with runtime + PG data
        logger = __import__("logging").getLogger(__name__)
        logger.warning(f"Checkpointer read failed for {task_id}: {e}")
        runtime = await load_task_artifacts(task_id) or {}
        pg_data = await _load_runtime_pg_data(task_id)
        return sanitize_for_output({
            "task": task_basic,
            "from": "partial",
            "error": f"Failed to read full state: {e}",
            "node_timeline": runtime.get("node_timeline") or [],
            "audit_log": runtime.get("audit_log") or [],
            "errors": runtime.get("errors") or [],
            "warnings": runtime.get("warnings") or [],
            **pg_data,
        })
