"""Human approval nodes — interrupt points for critical decisions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from wren_agent.models.task import ApprovalItem, ApprovalStatus, TaskPhase, TaskStatus
from wren_agent.state import AgentState

logger = logging.getLogger(__name__)


def _explore_was_truncated(state: AgentState) -> bool:
    return any(
        "AGENT_EXPLORE_MAX_TABLES" in str(warning)
        for warning in (state.get("warnings") or [])
    )


def _is_unconfirmed_table_removed(state: AgentState, drift) -> bool:
    """True when TABLE_REMOVED cannot be proven against the source catalog.

    Truncated light-explore (AGENT_EXPLORE_MAX_TABLES) used to treat every
    unexplored baseline table as deleted and then recreate a pending card
    after the user rejected it.
    """
    from wren_agent.models.task import DriftType

    if getattr(drift, "drift_type", None) != DriftType.TABLE_REMOVED:
        return False

    catalog_known = False
    for exploration in (state.get("explorations") or {}).values():
        catalog = list(getattr(exploration, "catalog_table_names", None) or [])
        if not catalog:
            continue
        catalog_known = True
        if drift.table_name in catalog:
            return True
    if catalog_known:
        return False
    return _explore_was_truncated(state)


_POST_EXEC_ARTIFACT = "final_validate"


def _append_post_execution_failure_item(
    state: AgentState, approval_items: list[ApprovalItem],
) -> None:
    """Open a pending card when final_validate failed after an already-approved plan.

    error_handling routes back to human_approval on empty materialization /
    unverifiable row counts. The design_plan item is already APPROVED, so
    pausing with only that card leaves the dashboard with nothing to click
    (task 4cfb5949). Approve retries deploy; reject goes to design_revise.
    """
    plan = state.get("design_plan")
    if plan is None or not getattr(plan, "approved", False):
        return
    if state.get("all_validations_passed") is not False:
        return
    results = state.get("execution_results") or []
    if not any(getattr(r, "success", False) for r in results):
        # Harness/design failures also leave all_validations_passed False
        # (the default). Only surface a card after a DAG actually ran.
        return
    if any(
        a.artifact_type == "execution"
        and a.artifact_id == _POST_EXEC_ARTIFACT
        and a.status == ApprovalStatus.PENDING
        for a in approval_items
    ):
        return
    msgs = [str(e) for e in (state.get("errors") or []) if e]
    detail = "\n".join(msgs[-5:]) if msgs else "Final validation did not pass."
    state["approval_context"] = "execution"
    approval_items.append(ApprovalItem(
        title="执行后校验未通过",
        description=(
            f"{detail}\n\n"
            "批准 = 重新部署并执行（适用于源数据已补齐的情况）；"
            "拒绝 = 打回设计修订。"
        ),
        artifact_type="execution",
        artifact_id=_POST_EXEC_ARTIFACT,
    ))


def _reconcile_unconfirmed_table_removed(state: AgentState) -> None:
    """Drop false TABLE_REMOVED drifts/cards so recover can reach design_ods."""
    drifts = list(state.get("schema_drifts") or [])
    dropped = [d for d in drifts if _is_unconfirmed_table_removed(state, d)]
    if not dropped:
        return

    drop_tables = {d.table_name for d in dropped}
    state["schema_drifts"] = [d for d in drifts if d not in dropped]
    state["approval_items"] = [
        item for item in (state.get("approval_items") or [])
        if not (
            item.artifact_type == "drift_fix" and item.artifact_id in drop_tables
        )
    ]
    preview = ", ".join(sorted(drop_tables)[:8])
    extra = f" (+{len(drop_tables) - 8} more)" if len(drop_tables) > 8 else ""
    state["warnings"] = state.get("warnings", []) + [
        f"Dropped {len(dropped)} unconfirmed TABLE_REMOVED drift(s) "
        f"(unexplored ≠ deleted): {preview}{extra}"
    ]
    logger.info(
        "[%s] Reconciled %d unconfirmed TABLE_REMOVED drift(s)",
        state.get("task_id", "")[:8], len(dropped),
    )


async def human_approval(state: AgentState) -> AgentState:
    """Interrupt: present items for human approval.

    Approval checkpoints:
    1. Design plan (after harness validation passes)
    2. Drift fix proposals (after schema drift detection)
    3. Execution errors requiring escalation (after N retries or fatal errors)

    The graph pauses here until human approves/rejects.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.HUMAN_APPROVAL
    state["pending_human_input"] = True
    state["human_input_type"] = "approval"

    _reconcile_unconfirmed_table_removed(state)
    approval_items = state.get("approval_items", [])

    # Design plan approval
    design_plan = state.get("design_plan")
    if design_plan and not design_plan.approved:
        state["approval_context"] = "design_plan"
        approval_items.append(ApprovalItem(
            title="数仓建设方案审批",
            description=_build_design_plan_summary(state, design_plan),
            artifact_type="design_plan",
            artifact_id=design_plan.id,
        ))

    # Schema drift fix approval
    drifts = state.get("schema_drifts", [])
    for drift in drifts:
        if drift.is_breaking and not drift.human_approved_fix:
            state["approval_context"] = "schema_drift"
            if any(
                item.artifact_type == "drift_fix"
                and item.artifact_id == drift.table_name
                for item in approval_items
            ):
                continue
            sql_section = (
                f"\n建议修复 SQL:\n{drift.auto_fix_sql}"
                if drift.auto_fix_sql
                else "\n该变更没有可安全自动执行的修复 SQL，请确认后按当前源结构重新设计。"
            )
            approval_items.append(ApprovalItem(
                title=f"Schema 破坏性变更确认: {drift.table_name}",
                description=(
                    f"漂移类型: {drift.drift_type.value}\n"
                    f"详情: {drift.details}\n"
                    f"{sql_section}"
                ),
                artifact_type="drift_fix",
                artifact_id=drift.table_name,
            ))

    # Error escalation approval — only for ops-fixable errors (config /
    # dependency). Logic/data Unknown-column style failures are agent-repairable
    # via design_revise; opening an execution approval for them lets a human
    # "approve" unrepaired SQL and then hit the redeploy refuse guard
    # (task b131f22e).
    errors = state.get("error_classifications", [])
    for err in errors:
        if err.action != "escalate":
            continue
        err_type = getattr(getattr(err, "error_type", None), "value", "")
        if err_type in ("data", "logic"):
            continue
        state["approval_context"] = "execution"
        approval_items.append(ApprovalItem(
            title=f"DAG 执行错误: {err.task_instance_id}",
            description=(
                f"错误类型: {err.error_type.value}\n"
                f"摘要: {err.summary}\n"
                f"建议操作: 人工介入处理\n"
                f"原始日志(截断): {err.raw_log[:500]}"
            ),
            artifact_type="execution",
            artifact_id=err.task_instance_id,
        ))

    state["approval_items"] = approval_items

    # Recovery guard: sync any approval decisions persisted to PG before crash.
    # If the crash happened AFTER the human approved/rejected via API but BEFORE
    # human_approval finished processing, PG has the decision but the checkpointed
    # state may not.
    recovery_count = state.get("recovery_count", 0)
    if recovery_count > 0:
        await _sync_approval_responses_from_pg(state)

    _append_post_execution_failure_item(state, approval_items)
    state["approval_items"] = approval_items

    # Never expose an approval state with zero PENDING items. Already-approved
    # cards from an earlier checkpoint are not actionable — pausing on them
    # leaves the dashboard with nothing to click (task 4cfb5949).
    # Exception: schema_drift re-entry after false TABLE_REMOVED cards were
    # dropped (truncated explore) — continue to design_ods instead of failing.
    # Recovery after the human already decided: don't re-pause, let
    # process_approval consume the synced statuses.
    pending_items = [
        a for a in approval_items if a.status == ApprovalStatus.PENDING
    ]
    if not pending_items:
        if state.get("approval_context") == "schema_drift":
            state["pending_human_input"] = False
            state["human_input_type"] = ""
            return state
        if recovery_count > 0 and approval_items:
            # Human already decided via API before the crash; process_approval
            # is next. Re-pausing would show stale approved cards with no
            # buttons.
            state["pending_human_input"] = False
            state["human_input_type"] = ""
            return state
        message = (
            "Approval requested without any approvable items; "
            "the upstream design or error-classification result is missing"
        )
        state["status"] = TaskStatus.FAILED
        state["pending_human_input"] = False
        state["human_input_type"] = ""
        if message not in state.get("errors", []):
            state["errors"] = state.get("errors", []) + [message]
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "human_approval",
            "action": "failed_no_approval_items",
            "timestamp": now.isoformat(),
        }]

        from wren_agent.engine import update_task_pg_status
        from wren_agent.tools.event_emitter import emit_error, emit_status
        emit_error(state, message)
        emit_status(state, TaskStatus.FAILED.value, "awaiting_approval")
        await update_task_pg_status(
            state["task_id"], TaskStatus.FAILED.value, "human_approval"
        )
        return state

    # Persist new approval items to PG table for API access
    for item in approval_items:
        if getattr(item, '_persisted', False):
            continue
        try:
            from wren_agent.tools.pg_store import get_pg_pool
            pool = get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO task_approvals "
                    "(id, task_id, title, description, artifact_type, artifact_id, status, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, NOW()) "
                    "ON CONFLICT (id) DO NOTHING",
                    str(item.id), state["task_id"], item.title, item.description or "",
                    item.artifact_type or "", item.artifact_id, "pending",
                )
            item._persisted = True
        except Exception as e:
            logger.warning(f"Failed to persist approval to PG: {e}")

    # ── Fire background narratives for the design_plan approval (T-01) ──
    # Runs while the task pauses; auto mode generates the same way. The
    # computed risk block persists here; LLM blocks land when ready.
    design_plan_item = next(
        (a for a in approval_items if a.artifact_type == "design_plan"),
        None,
    )
    if design_plan_item and getattr(design_plan_item, "_persisted", False):
        try:
            from wren_agent.tools.approval_narratives import fire_approval_narratives
            await fire_approval_narratives(state, str(design_plan_item.id))
        except Exception as e:
            logger.warning(
                f"[{state['task_id'][:8]}] narrative generation fire failed: {e}"
            )

    # ── Auto mode: approve everything ourselves, never pause ──
    from wren_agent.state import is_auto_mode
    if is_auto_mode(state):
        pending_items = [
            a for a in approval_items if a.status == ApprovalStatus.PENDING
        ]
        exec_items = [a for a in pending_items if a.artifact_type == "execution"]
        rounds = state.get("auto_approval_rounds", 0)

        # CONFIG errors (worker environment / task definition problems) cannot
        # be fixed by re-running or regenerating SQL — fail fast instead of
        # burning auto-approval rounds on identical failures.
        from wren_agent.models.task import ErrorType
        exec_ids = {a.artifact_id for a in exec_items}
        exec_errors = [
            c for c in state.get("error_classifications", [])
            if c.task_instance_id in exec_ids
        ]
        all_config = bool(exec_errors) and all(
            c.error_type == ErrorType.CONFIG for c in exec_errors
        )
        repairable_exec = [
            c for c in exec_errors
            if c.error_type in (ErrorType.LOGIC, ErrorType.DATA)
        ]
        post_exec_empty = [
            a for a in exec_items if a.artifact_id == _POST_EXEC_ARTIFACT
        ]

        if exec_items and (
            rounds >= 2 or all_config or repairable_exec or post_exec_empty
        ):
            # Guard: never auto-approve unrepaired SQL (logic/data) — that
            # redeploys the same broken statements. Config errors and
            # persistent failures after 2 rounds also fail fast. Empty
            # materialization (task 4cfb5949) is the same class of loop.
            from wren_agent.tools.event_emitter import emit_error, emit_status
            from wren_agent.engine import persist_approval_response, update_task_pg_status

            if post_exec_empty:
                msg = (
                    "auto mode: refusing to auto-approve a failed final "
                    "validation (empty or unverifiable materialization) — "
                    "failing the task instead of redeploying the same SQL"
                )
            elif repairable_exec and rounds < 2 and not all_config:
                msg = (
                    "auto mode: refusing to auto-approve unrepaired SQL "
                    "execution errors (would redeploy the same statements) — "
                    f"failing the task. First error: {repairable_exec[0].summary}"
                )
            elif all_config and rounds < 2:
                msg = (
                    "auto mode: execution errors are configuration/environment "
                    "problems (e.g. task never started on the DS worker) — "
                    "re-running cannot fix them, failing the task immediately. "
                    f"First error: {exec_errors[0].summary}"
                )
            else:
                msg = (
                    "auto mode: execution errors persisted after 2 auto-approved rounds — "
                    "failing the task (manual mode required to resolve these errors)"
                )
            state["errors"] = state.get("errors", []) + [msg]
            state["status"] = TaskStatus.FAILED
            comment = "[auto] 连续执行失败，自动拒绝并终止任务"
            for a in pending_items:
                a.status = ApprovalStatus.REJECTED
                a.comment = comment
                a.resolved_at = now
                await persist_approval_response(
                    state["task_id"], str(a.id), False, comment,
                )
            emit_error(state, msg)
            emit_status(state, TaskStatus.FAILED.value)
            await update_task_pg_status(
                state["task_id"], TaskStatus.FAILED.value, "human_approval",
            )
            logger.error(f"[{state['task_id'][:8]}] {msg}")
            return state
        if exec_items:
            state["auto_approval_rounds"] = rounds + 1
        return await _auto_approve_items(state, pending_items, now)

    from wren_agent.tools.event_emitter import emit_phase, emit_approval
    emit_phase(state, "human_approval")
    for item in pending_items[-3:]:
        emit_approval(state, item.title, len(pending_items))

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "human_approval",
        "action": "awaiting_approval",
        "items_count": len(pending_items),
        "timestamp": now.isoformat(),
    }]

    # Emit interrupt event to WebSocket
    from wren_agent.tools.event_emitter import emit_interrupt
    emit_interrupt(state, "approval", {
        "items_count": len(pending_items),
        "items": [
            {"id": str(a.id), "title": a.title[:200], "artifact_type": a.artifact_type}
            for a in pending_items[-5:]
        ],
    })

    # Update PG status before pausing
    from wren_agent.engine import (
        _task_pauses, update_task_pg_status, wait_for_resume,
    )
    await update_task_pg_status(state["task_id"], "awaiting_approval", "human_approval")

    # ACTUAL pause: register in PG (migration 010) so ANY engine instance can
    # approve — in-process event fast path + PG poll for cross-engine.
    from wren_agent.tools.pg_store import clear_task_pause, pause_task
    await pause_task(state["task_id"], "approval")
    pause_ev = asyncio.Event()
    _task_pauses[state["task_id"]] = pause_ev

    payload = await wait_for_resume(state["task_id"], pause_ev, timeout=1800)

    # After resume — apply the human decision, then reconcile from PG.
    # Bulk approve persists every pending row first and sends {bulk: true}
    # so one resume can settle a hundred drift cards instead of one-by-one.
    if payload:
        comment = payload.get("comment", "")
        now_resolved = datetime.now(timezone.utc)
        if payload.get("bulk"):
            approved = payload.get("approved", True)
            new_status = (
                ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            )
            for a in state.get("approval_items", []):
                if a.status == ApprovalStatus.PENDING:
                    a.status = new_status
                    a.comment = comment
                    a.resolved_at = now_resolved
        else:
            aid = payload.get("aid", "")
            approved = payload.get("approved", False)
            if aid:
                for a in state.get("approval_items", []):
                    if str(a.id) == aid:
                        a.status = (
                            ApprovalStatus.APPROVED if approved
                            else ApprovalStatus.REJECTED
                        )
                        a.comment = comment
                        a.resolved_at = now_resolved
        await _sync_approval_responses_from_pg(state)
        state["pending_human_input"] = False

    _task_pauses.pop(state["task_id"], None)
    try:
        await clear_task_pause(state["task_id"])
    except Exception:
        logger.debug("clear_task_pause failed", exc_info=True)
    if state.get("approval_context") == "schema_drift":
        await update_task_pg_status(state["task_id"], "designing", "process_approval")
    else:
        await update_task_pg_status(state["task_id"], "deploying", "dag_deploy")

    return state


def _build_design_plan_summary(state: AgentState, design_plan) -> str:
    """Structured approval-card summary (T-02): goal digest + layer table
    counts + SQL count + new metric count + risk markers.

    Pure structure — no LLM (the card renders the instant the task pauses,
    before narratives are ready).
    """
    ods_count = len(design_plan.ods_designs or [])
    dim_count = len(design_plan.dim_designs or [])
    dwd_count = len(design_plan.dwd_designs or [])
    dws_count = len(design_plan.dws_designs or [])
    ads_count = len(design_plan.ads_designs or [])
    sql_count = len(design_plan.sql_artifacts or [])

    # New metrics: DWS measures + ADS metrics, deduped by name
    metric_names = set()
    for d in design_plan.dws_designs or []:
        for m in d.measures or []:
            if m.get("name"):
                metric_names.add(m["name"])
    for d in design_plan.ads_designs or []:
        for m in d.metrics or []:
            if m.get("name"):
                metric_names.add(m["name"])
    metric_count = len(metric_names)

    goal = state.get("modeling_goal", "") or ""
    goal_summary = goal[:60] + ("…" if len(goal) > 60 else "")

    lines = [
        f"目标: {goal_summary}",
        f"方案: ODS({ods_count})+DIM({dim_count})+DWD({dwd_count})+"
        f"DWS({dws_count})+ADS({ads_count}) 共 {ods_count + dim_count + dwd_count + dws_count + ads_count} 表",
        f"SQL 产物: {sql_count} 条",
        f"新指标: {metric_count} 个",
    ]
    risk_markers = []
    if not state.get("all_validations_passed", False):
        risk_markers.append(f"校验未通过已修订 {state.get('design_revision_count', 0)} 轮")
    if state.get("insufficient_gaps"):
        risk_markers.append(f"数仓缺口 {len(state['insufficient_gaps'])} 项")
    if state.get("schema_drifts"):
        risk_markers.append(f"Schema drift {len(state['schema_drifts'])} 项")
    if risk_markers:
        lines.append("⚠ " + " / ".join(risk_markers))
    return "\n".join(lines)


async def _auto_approve_items(
    state: AgentState, pending_items: list[ApprovalItem], now: datetime
) -> AgentState:
    """Auto mode: approve all pending items and skip the interrupt/pause."""
    from wren_agent.engine import persist_approval_response, update_task_pg_status
    from wren_agent.tools.event_emitter import emit, emit_phase, Events

    comment = "[auto] 自动批准（auto mode）"
    for a in pending_items:
        a.status = ApprovalStatus.APPROVED
        a.comment = comment
        a.resolved_at = now
        await persist_approval_response(state["task_id"], str(a.id), True, comment)
        emit(state, Events.APPROVAL_RESOLVED, {
            "id": str(a.id),
            "title": a.title[:200],
            "artifact_type": a.artifact_type,
            "approved": True,
            "auto": True,
        })

    state["pending_human_input"] = False
    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "human_approval",
        "action": "auto_approved",
        "count": len(pending_items),
        "timestamp": now.isoformat(),
    }]
    emit_phase(state, "human_approval")
    if state.get("approval_context") == "schema_drift":
        await update_task_pg_status(state["task_id"], "designing", "process_approval")
    else:
        await update_task_pg_status(state["task_id"], "deploying", "dag_deploy")

    logger.info(
        f"[{state['task_id'][:8]}] Auto mode: {len(pending_items)} approval item(s) auto-approved"
    )
    return state


# (process_approval: no interrupt needed — it runs after human responds)


async def _register_approved_assets(state: AgentState) -> None:
    """Register approved ADS metrics and DIM dimensions in the registries.

    register_metric/register_dimension previously had ZERO call sites —
    "Registered after approval" was a comment, not code — so
    metric_registry / dimension_registry stayed empty and the ADS prompt's
    registry context was always blank (task a1af4bed investigation).
    Idempotent per task; failures only warn — registry issues must never
    block deployment.
    """
    try:
        from wren_agent.tools.pg_store import (
            get_pg_pool,
            register_dimension,
            register_metric,
        )
        plan = state.get("design_plan")
        task_id = state.get("task_id", "")
        if plan is None or not task_id:
            return
        pool = get_pg_pool()
        registered = 0

        # Metrics from approved ADS designs. The source DWS table may be bare
        # (ADS source_dws_tables mixes bare and db-qualified names) — qualify
        # it with the dws-layer database so metric_registry.source_table always
        # carries a consistent db prefix for db-scoped filtering (P3).
        from wren_agent.nodes.deployment import registry_db_name
        for ads in (plan.ads_designs or []):
            source = (ads.source_dws_tables or [""])[0] or ""
            if source and "." not in source:
                dws_db = registry_db_name(state, source, "dws")
                if dws_db:
                    source = f"{dws_db}.{source}"
            grain = ",".join(ads.dimensions or [])
            for m in (ads.metrics or []):
                if not isinstance(m, dict):
                    continue
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                async with pool.acquire() as conn:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM metric_registry WHERE owner_task_id=$1 "
                        "AND metric_name=$2 AND active=TRUE LIMIT 1",
                        task_id, name,
                    )
                if exists:
                    continue
                result = await register_metric(
                    metric_name=name,
                    metric_type=str(m.get("metric_type") or "atomic"),
                    definition_sql=str(m.get("expression") or ""),
                    source_table=source,
                    owner_task_id=task_id,
                    grain=grain,
                    parent_metrics=[str(p) for p in (m.get("parent_metrics") or [])],
                    tags=["auto"],
                )
                # Only "created" actually inserted a row — "conflict" means the
                # candidate was refused (similar name/definition mismatch) and
                # must not count as a registered asset.
                if result.get("status") == "created":
                    registered += 1

        # Dimensions from approved DIM designs (empty in warehouse mode).
        # db_name prefers the explored real schema over the configured dim
        # layer db — the same source of truth the backfill script uses, so
        # registration and history-scan/backfill share one db scope (P1).
        for dim in (plan.dim_designs or []):
            dim_db = registry_db_name(state, dim.dim_table, "dim")
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT 1 FROM dimension_registry WHERE owner_task_id=$1 "
                    "AND dim_table=$2 AND db_name=$3 AND active=TRUE LIMIT 1",
                    task_id, dim.dim_table, dim_db,
                )
            if exists:
                continue
            result = await register_dimension(
                dim_name=dim.dim_name,
                dim_table=dim.dim_table,
                owner_task_id=task_id,
                natural_keys=list(dim.natural_key or []),
                grain=",".join(str(g) for g in (dim.natural_key or [])),
                fields=[f for f in (dim.fields or [])],
                db_name=dim_db,
            )
            # Only "created" inserted a row — conflict is refused, not registered.
            if result.get("status") == "created":
                registered += 1

        if registered:
            state["audit_log"] = state.get("audit_log", []) + [{
                "phase": "process_approval",
                "action": "registered_assets",
                "count": registered,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]
    except Exception as exc:
        state["warnings"] = state.get("warnings", []) + [
            f"Asset registration skipped: {str(exc)[:200]}"
        ]
        logger.warning("register_approved_assets failed: %s", exc)


async def process_approval(state: AgentState) -> AgentState:
    """Process human approval decisions.

    Handles two outcomes:
    - All approved → proceed to next phase (dag_deploy)
    - Any rejected → go back to design revision (design_revise)

    Exception: approving unrepaired SQL execution errors (logic/data) marks
    the task FAILED — redeploying the same statements is not progress.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["pending_human_input"] = False

    approvals = state.get("approval_items", [])
    rejected = [a for a in approvals if a.status == ApprovalStatus.REJECTED]
    approved = [a for a in approvals if a.status == ApprovalStatus.APPROVED]

    # Mark design plan as approved if its approval was granted
    design_plan = state.get("design_plan")
    for item in approved:
        if item.artifact_type == "design_plan" and design_plan:
            design_plan.approved = True
            design_plan.approved_at = now
            # Populate metric_registry / dimension_registry from the approved
            # designs — the registries were never written (see
            # _register_approved_assets), so the ADS prompt's registry context
            # was always empty.
            await _register_approved_assets(state)

        if item.artifact_type == "drift_fix":
            # Mark drift fix as approved
            for drift in state.get("schema_drifts", []):
                if drift.table_name == item.artifact_id:
                    drift.human_approved_fix = True

    if (
        state.get("approval_context") == "execution"
        and not rejected
        and approved
    ):
        repairable = [
            e for e in (state.get("error_classifications") or [])
            if getattr(getattr(e, "error_type", None), "value", "") in ("data", "logic")
        ]
        if repairable:
            from wren_agent.models.task import TaskStatus
            msg = (
                "Execution errors were approved without a SQL repair — "
                "refusing to redeploy the same statements. "
                f"Last error: {repairable[0].summary}"
            )
            if msg not in state.get("errors", []):
                state["errors"] = state.get("errors", []) + [msg]
            state["status"] = TaskStatus.FAILED
            state["failure_kind"] = "execution"

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "process_approval",
        "action": "processed",
        "approved": len(approved),
        "rejected": len(rejected),
        "timestamp": now.isoformat(),
    }]

    return state


async def _sync_approval_responses_from_pg(state: AgentState) -> None:
    """On recovery, pull any approval decisions persisted to PG before crash.

    If the crash happened AFTER the human approved/rejected via API but BEFORE
    human_approval finished processing, the PG table has the decision but the
    checkpointed state may not. This sync prevents re-pausing on already-decided items.
    """
    try:
        from wren_agent.tools.pg_store import get_pg_pool
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status, comment FROM task_approvals "
                "WHERE task_id=$1 AND status IN ('approved', 'rejected')",
                state["task_id"],
            )
        for row in rows:
            for a in state.get("approval_items", []):
                if str(a.id) == str(row["id"]):
                    a.status = (
                        ApprovalStatus.APPROVED
                        if row["status"] == "approved"
                        else ApprovalStatus.REJECTED
                    )
                    a.comment = row.get("comment", "")
                    from datetime import datetime, timezone
                    a.resolved_at = datetime.now(timezone.utc)
    except Exception:
        pass  # Non-blocking — recovery continues regardless
