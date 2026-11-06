"""LangGraph state machine definition for the Wren Modeling Agent."""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from wren_agent.state import AgentState, create_initial_state, is_ods_migration_task

logger = logging.getLogger(__name__)

# Max design_revise rounds before escalating to human approval (loop guard)
MAX_DESIGN_REVISIONS = 3


# ── Routing functions ───────────────────────────────────────────


def _has_pending_blocking_clarifications(state: AgentState) -> bool:
    """Check if there are unanswered blocking clarifications."""
    return any(
        c.blocking and c.status.value == "pending"
        for c in state.get("clarifications", [])
    )


def _has_pending_deep_explore(state: AgentState) -> bool:
    """Check if there are deep exploration items pending human confirmation."""
    return len(state.get("deep_explore_pending", [])) > 0


def _has_pending_approval(state: AgentState) -> bool:
    """Check if there are pending approval items."""
    return any(
        a.status.value == "pending" for a in state.get("approval_items", [])
    )


def _all_approvals_granted(state: AgentState) -> bool:
    """Check if all approval items are approved."""
    items = state.get("approval_items", [])
    if not items:
        return True
    return all(a.status.value == "approved" for a in items)


def _has_schema_drifts(state: AgentState) -> bool:
    """Check if any breaking schema drifts were detected."""
    return any(d.is_breaking for d in state.get("schema_drifts", []))


# ── Routing helpers ─────────────────────────────────────────────


def route_after_schema_drift(state: AgentState) -> Literal["generate_drift_fixes", "design_ods", "__end__"]:
    if _has_schema_drifts(state):
        return "generate_drift_fixes"
    return "design_ods"


def route_after_ods_design(state: AgentState) -> Literal["design_dim", "harness_validate"]:
    """Select the first layer for the requested business task type."""
    if is_ods_migration_task(state):
        return "harness_validate"
    return "design_dim"


def route_after_dim_design(state: AgentState) -> Literal["design_dwd"]:
    return "design_dwd"


def route_after_dwd_design(state: AgentState) -> Literal["design_dws", "harness_validate"]:
    if is_ods_migration_task(state) or state.get("business_task_type") == "core_model":
        return "harness_validate"
    return "design_dws"


def route_after_drift_fix(state: AgentState) -> Literal["human_approval", "design_ods", "__end__"]:
    if _has_schema_drifts(state):
        return "human_approval"
    return "design_ods"


def route_after_schema_explore(state: AgentState) -> Literal["detect_sensitive_fields", "grill_clarifications", "generate_report", "__end__"]:
    # Fail-fast: exploration found nothing usable (bad credentials, unregistered
    # datasource, ...) — designing on an empty state only produces empty layers.
    # FAILED tasks still get a plain-language failure report.
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"
    # After light exploration, detect sensitives, then proceed to grill
    return "detect_sensitive_fields"


def route_after_sensitive_fields(state: AgentState) -> Literal["confirm_sensitive", "human_confirm_deep", "__end__"]:
    if state.get("sensitive_fields"):
        return "confirm_sensitive"
    return "human_confirm_deep"


def route_after_deep_explore(state: AgentState) -> Literal["deep_explore", "grill_clarifications", "__end__"]:
    # Called from both human_confirm_deep and (optionally) deep_explore
    if _has_pending_deep_explore(state):
        return "deep_explore"
    return "grill_clarifications"


def route_after_grill(state: AgentState) -> Literal["wait_human", "detect_schema_drifts", "__end__"]:
    if _has_pending_blocking_clarifications(state):
        return "wait_human"
    return "detect_schema_drifts"


def route_after_human_response(state: AgentState) -> Literal["wait_human", "detect_schema_drifts", "__end__"]:
    """Called after wait_human — check if more blocking clarifications remain."""
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.CANCELLED:
        return "__end__"
    if _has_pending_blocking_clarifications(state):
        return "wait_human"
    return "detect_schema_drifts"


def route_after_design(state: AgentState) -> Literal["wait_human", "harness_validate", "generate_report", "__end__"]:
    """After design_ads — warehouse mode may raise blocking clarifications
    (insufficient_warehouse, T-05) that interrupt before validation; a task
    cancelled via 放弃 ends here, a failed one still gets a failure report.
    """
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.CANCELLED:
        return "__end__"
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"
    if _has_pending_blocking_clarifications(state):
        return "wait_human"
    return "harness_validate"


def route_after_approval(state: AgentState) -> Literal["human_approval", "design_ods", "dag_deploy", "design_revise", "generate_report", "__end__"]:
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"
    if _has_pending_approval(state):
        return "human_approval"
    if state.get("approval_context") == "schema_drift":
        rejected_drift = any(
            a.artifact_type == "drift_fix" and a.status.value == "rejected"
            for a in state.get("approval_items", [])
        )
        if rejected_drift:
            # NOTE: state mutations here are discarded by LangGraph — the
            # generate_report node detects this case and owns the FAILED flag.
            return "generate_report"
        return "design_ods"
    # Execution-error approvals must not blindly redeploy the same SQL
    # (task 57d84a75 / logic Unknown-column loop).
    if state.get("approval_context") == "execution":
        errors = state.get("error_classifications") or []
        repairable = [
            e for e in errors
            if getattr(getattr(e, "error_type", None), "value", "") in ("data", "logic")
        ]
        if _all_approvals_granted(state):
            if repairable:
                return "generate_report"
            return "dag_deploy"  # config/dependency — ops may have fixed the env
        if state.get("design_revision_count", 0) < MAX_DESIGN_REVISIONS:
            return "design_revise"
        return "generate_report"
    if _all_approvals_granted(state):
        return "dag_deploy"
    return "design_revise"


def route_after_validation(state: AgentState) -> Literal["human_approval", "design_revise", "generate_report", "dag_deploy", "__end__"]:
    # Fail-fast cases (missing plan, empty artifacts, revision budget
    # exhausted) are marked FAILED inside harness_validate itself — state
    # mutations in route functions are discarded by LangGraph, so the node
    # owns them. Failed tasks still get a plain-language failure report.
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"

    if state.get("all_validations_passed", False):
        # After an execution-error → design_revise cycle the plan is already
        # approved. Re-entering human_approval re-surfaces stale escalate
        # errors and approving them hits the "refuse to redeploy" guard
        # (task b131f22e) — redeploy the repaired SQL instead.
        plan = state.get("design_plan")
        if plan is not None and getattr(plan, "approved", False):
            return "dag_deploy"
        return "human_approval"
    # Loop guard: after MAX_DESIGN_REVISIONS failed revisions, escalate to human
    # instead of ping-ponging design_revise ↔ harness_validate until the
    # recursion limit kills the whole task (GRAPH_RECURSION_LIMIT).
    if state.get("design_revision_count", 0) >= MAX_DESIGN_REVISIONS:
        return "human_approval"
    # If design phase validation failed, go back to design
    return "design_revise"


def route_after_deploy(state: AgentState) -> Literal["execute_tasks", "error_handling", "generate_report", "__end__"]:
    """After dag_deploy: run DAGs on success, classify on failure, stop when the
    auto-mode deploy circuit breaker (MAX_DEPLOY_FAILURES) has tripped.
    Failed tasks still get a plain-language failure report."""
    from wren_agent.models.task import TaskStatus
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"
    if not state.get("deploy_succeeded", False):
        return "error_handling"
    return "execute_tasks"


def route_after_execution(state: AgentState) -> Literal["classify_errors", "final_validate", "__end__"]:
    """Check if all executions passed."""
    results = state.get("execution_results", [])
    # `all([])` is vacuously True — an empty result list means nothing ran,
    # which is a failure, not a pass.
    if results and all(r.success for r in results):
        return "final_validate"
    return "classify_errors"


def route_after_error_classify(state: AgentState) -> Literal["retry_execution", "human_approval", "wait_human", "dag_deploy", "final_validate", "design_revise", "generate_report", "__end__"]:
    """Route based on error classification results."""
    from wren_agent.models.task import TaskStatus

    # classify_errors may have already marked FAILED (repair budget exhausted).
    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"

    errors = state.get("error_classifications", [])
    if not errors:
        return "final_validate"

    # Check if any require escalation (approval from human)
    transient_errors = [e for e in errors if e.error_type.value == "transient"]
    resource_errors = [e for e in errors if e.error_type.value == "resource"]
    repairable_errors = [e for e in errors if e.error_type.value in ("data", "logic")]
    fatal_errors = [e for e in errors if e.error_type.value in ("config", "dependency")]

    if transient_errors:
        retry_count = state.get("retry_count", 0)
        max_retries = state.get("max_retries", 3)
        if retry_count < max_retries:
            return "retry_execution"
        else:
            return "human_approval"  # Max retries exhausted

    if resource_errors:
        return "wait_human"  # Wait for human to resolve resources

    # SQL logic/data errors are agent-fixable via design_revise. When the
    # revision budget is exhausted, fail the task — do NOT open human_approval
    # that can redeploy the same broken SQL (approval → dag_deploy loop).
    if repairable_errors:
        if state.get("design_revision_count", 0) < MAX_DESIGN_REVISIONS:
            return "design_revise"
        return "generate_report"

    if fatal_errors:
        return "human_approval"  # Escalate to human (env/config may be fixable)

    return "dag_deploy"  # Shouldn't reach here normally


def route_after_final_validate(state: AgentState) -> Literal["quality_monitor", "error_handling", "__end__"]:
    # Success path runs data-quality checks before reporting (README flow:
    # final_validate → quality_monitor → generate_report).
    if state.get("all_validations_passed", False):
        return "quality_monitor"
    return "error_handling"


def route_after_error_handling(state: AgentState) -> Literal["human_approval", "generate_report", "__end__"]:
    """Stop exhausted recovery instead of opening approval for a failed task.
    Failed tasks still get a plain-language failure report."""
    from wren_agent.models.task import TaskStatus

    if state.get("status") == TaskStatus.FAILED:
        return "generate_report"
    return "human_approval"


def route_after_quality_monitor(state: AgentState) -> Literal["generate_report"]:
    """Set the truthful outcome, then generate a report for pass or failure."""
    from wren_agent.models.task import TaskStatus

    quality = state.get("quality_checks", {}) or {}
    if quality.get("failed", 0) > 0 or quality.get("errored", 0) > 0:
        state["status"] = TaskStatus.FAILED
    else:
        state["status"] = TaskStatus.COMPLETED
    return "generate_report"


def route_after_report(state: AgentState) -> Literal["export_kg", "__end__"]:
    """Failed tasks end after their failure report — exporting a KG of a
    design that never materialized would pollute the asset map."""
    from wren_agent.models.task import TaskStatus

    if state.get("status") == TaskStatus.FAILED:
        return "__end__"
    return "export_kg"


# ── Graph builder ────────────────────────────────────────────────


def build_modeling_agent_graph(checkpointer=None) -> CompiledStateGraph:
    """Build the LangGraph state machine for the Wren Modeling Agent.

    Args:
        checkpointer: Optional LangGraph checkpointer (e.g. AsyncPostgresSaver).
                      When provided, state is auto-persisted at every superstep,
                      enabling crash recovery via graph.astream(None, config).
                      Defaults to None (bare graph, backward compatible).
    """

    builder = StateGraph(AgentState)

    # ── Nodes ─────────────────────────────────────────────────
    # Phase: Task initialization
    builder.add_node("receive_task", _receive_task)
    builder.add_node("blueprint_match", _blueprint_match)

    # Phase: Schema exploration
    builder.add_node("schema_explore_light", _schema_explore_light)
    builder.add_node("detect_sensitive_fields", _detect_sensitive_fields)
    builder.add_node("confirm_sensitive", _confirm_sensitive)
    builder.add_node("deep_explore", _deep_explore)
    builder.add_node("human_confirm_deep", _human_confirm_deep)

    # Phase: Human grilling
    builder.add_node("grill_clarifications", _grill_clarifications)
    builder.add_node("wait_human", _wait_human)

    # Phase: Schema drift detection (before ODS design)
    builder.add_node("detect_schema_drifts", _detect_schema_drifts)
    builder.add_node("generate_drift_fixes", _generate_drift_fixes)

    # Phase: Layer design
    builder.add_node("design_ods", _design_ods)
    builder.add_node("design_dim", _design_dim)
    builder.add_node("design_dwd", _design_dwd)
    builder.add_node("design_dws", _design_dws)
    builder.add_node("design_ads", _design_ads)

    # Phase: Harness validation
    builder.add_node("harness_validate", _harness_validate)
    builder.add_node("design_revise", _design_revise)

    # Phase: Approval
    builder.add_node("human_approval", _human_approval)
    builder.add_node("process_approval", _process_approval)

    # Phase: Deployment
    builder.add_node("dag_deploy", _dag_deploy)

    # Phase: Execution
    builder.add_node("execute_tasks", _execute_tasks)
    builder.add_node("classify_errors", _classify_errors)
    builder.add_node("retry_execution", _retry_execution)

    # Phase: Final validation
    builder.add_node("final_validate", _final_validate)
    builder.add_node("error_handling", _error_handling)
    builder.add_node("quality_monitor", _quality_monitor)

    # Phase: Reporting
    builder.add_node("generate_report", _generate_report)
    builder.add_node("export_kg", _export_kg)

    # ── Edges ─────────────────────────────────────────────────
    # Entry point
    builder.set_entry_point("receive_task")

    # Task init -> Blueprint match -> Schema exploration
    builder.add_edge("receive_task", "blueprint_match")
    builder.add_edge("blueprint_match", "schema_explore_light")

    # Schema exploration → Sensitive fields detection
    builder.add_conditional_edges("schema_explore_light", route_after_schema_explore)
    builder.add_conditional_edges("detect_sensitive_fields", route_after_sensitive_fields)

    # Deep explore flow: confirm_sensitive → human_confirm_deep (interrupt) → deep_explore → grill_clarifications
    builder.add_edge("confirm_sensitive", "human_confirm_deep")
    builder.add_conditional_edges("human_confirm_deep", route_after_deep_explore)
    builder.add_edge("deep_explore", "grill_clarifications")

    # Grill -> human interaction or design
    builder.add_conditional_edges("grill_clarifications", route_after_grill)
    # wait_human → route based on what human answered
    builder.add_conditional_edges("wait_human", route_after_human_response)

    # After human responds to clarifications, check: more questions → grill; ready → design
    builder.add_conditional_edges("process_approval", route_after_approval)

    # Schema drift detection before ODS
    builder.add_conditional_edges("detect_schema_drifts", route_after_schema_drift)
    builder.add_conditional_edges("generate_drift_fixes", route_after_drift_fix)

    # Design pipeline (sequential, per the spec)
    builder.add_conditional_edges("design_ods", route_after_ods_design)
    builder.add_conditional_edges("design_dim", route_after_dim_design)
    builder.add_conditional_edges("design_dwd", route_after_dwd_design)
    builder.add_edge("design_dws", "design_ads")

    # Validation after design
    builder.add_conditional_edges("design_ads", route_after_design)
    builder.add_conditional_edges("harness_validate", route_after_validation)

    # Approval: human_approval → process_approval (human_action → check_result)
    builder.add_edge("human_approval", "process_approval")

    # Revise design and re-validate
    builder.add_edge("design_revise", "harness_validate")

    # Deployment
    builder.add_conditional_edges("dag_deploy", route_after_deploy)

    # Execution & error handling
    builder.add_conditional_edges("execute_tasks", route_after_execution)
    builder.add_conditional_edges("classify_errors", route_after_error_classify)

    # Retry loop
    builder.add_edge("retry_execution", "execute_tasks")

    # Final validation
    builder.add_conditional_edges("final_validate", route_after_final_validate)
    builder.add_conditional_edges("error_handling", route_after_error_handling)

    # Quality → Report
    builder.add_conditional_edges("quality_monitor", route_after_quality_monitor)

    # Reporting
    builder.add_conditional_edges("generate_report", route_after_report)
    builder.add_edge("export_kg", END)

    return builder.compile(checkpointer=checkpointer)


def build_modeling_agent_graph_with_interrupts() -> CompiledStateGraph:
    """Build graph with interrupt-before human-wait nodes.

    LangGraph pauses execution BEFORE these nodes.
    Human resumes via API → Command(resume=...) → graph continues.
    """
    builder = _build_builder()
    builder.set_entry_point("receive_task")
    # ... (same edge setup as build_modeling_agent_graph)
    # The key difference: interrupt_before
    return builder.compile(
        interrupt_before=["human_confirm_deep", "wait_human", "human_approval"],
    )


def _build_builder() -> StateGraph:
    """Rebuild the builder with all nodes registered."""
    builder = StateGraph(AgentState)

    from wren_agent.tools.node_tracer import wrap_node_with_trace

    from wren_agent.nodes.receive_task import receive_task
    from wren_agent.nodes.blueprint_match import blueprint_match
    from wren_agent.nodes.explore import (
        schema_explore_light, detect_sensitive_fields, confirm_sensitive,
        deep_explore, human_confirm_deep,
    )
    from wren_agent.nodes.grill import grill_clarifications, wait_human
    from wren_agent.nodes.deployment import (
        classify_errors, dag_deploy, detect_schema_drifts,
        error_handling, execute_tasks, generate_drift_fixes, retry_execution,
    )
    from wren_agent.nodes.design_layers import (
        design_ads, design_dim, design_dwd, design_dws, design_ods,
    )
    from wren_agent.nodes.validation import (
        design_revise, final_validate, harness_validate,
    )
    from wren_agent.nodes.approval import human_approval, process_approval
    from wren_agent.nodes.reporting import export_kg, generate_report
    from wren_agent.nodes.quality_monitor import quality_monitor

    # ── Nodes (with trace wrapping for observability) ──
    builder.add_node("receive_task", wrap_node_with_trace(receive_task, "receive_task"))
    builder.add_node("blueprint_match", wrap_node_with_trace(blueprint_match, "blueprint_match"))
    builder.add_node("schema_explore_light", wrap_node_with_trace(schema_explore_light, "schema_explore_light"))
    builder.add_node("detect_sensitive_fields", wrap_node_with_trace(detect_sensitive_fields, "detect_sensitive_fields"))
    builder.add_node("confirm_sensitive", wrap_node_with_trace(confirm_sensitive, "confirm_sensitive"))
    builder.add_node("deep_explore", wrap_node_with_trace(deep_explore, "deep_explore"))
    builder.add_node("human_confirm_deep", wrap_node_with_trace(human_confirm_deep, "human_confirm_deep"))
    builder.add_node("grill_clarifications", wrap_node_with_trace(grill_clarifications, "grill_clarifications"))
    builder.add_node("wait_human", wrap_node_with_trace(wait_human, "wait_human"))
    builder.add_node("detect_schema_drifts", wrap_node_with_trace(detect_schema_drifts, "detect_schema_drifts"))
    builder.add_node("generate_drift_fixes", wrap_node_with_trace(generate_drift_fixes, "generate_drift_fixes"))
    builder.add_node("design_ods", wrap_node_with_trace(design_ods, "design_ods"))
    builder.add_node("design_dim", wrap_node_with_trace(design_dim, "design_dim"))
    builder.add_node("design_dwd", wrap_node_with_trace(design_dwd, "design_dwd"))
    builder.add_node("design_dws", wrap_node_with_trace(design_dws, "design_dws"))
    builder.add_node("design_ads", wrap_node_with_trace(design_ads, "design_ads"))
    builder.add_node("harness_validate", wrap_node_with_trace(harness_validate, "harness_validate"))
    builder.add_node("design_revise", wrap_node_with_trace(design_revise, "design_revise"))
    builder.add_node("human_approval", wrap_node_with_trace(human_approval, "human_approval"))
    builder.add_node("process_approval", wrap_node_with_trace(process_approval, "process_approval"))
    builder.add_node("dag_deploy", wrap_node_with_trace(dag_deploy, "dag_deploy"))
    builder.add_node("execute_tasks", wrap_node_with_trace(execute_tasks, "execute_tasks"))
    builder.add_node("classify_errors", wrap_node_with_trace(classify_errors, "classify_errors"))
    builder.add_node("retry_execution", wrap_node_with_trace(retry_execution, "retry_execution"))
    builder.add_node("final_validate", wrap_node_with_trace(final_validate, "final_validate"))
    builder.add_node("error_handling", wrap_node_with_trace(error_handling, "error_handling"))
    builder.add_node("quality_monitor", wrap_node_with_trace(quality_monitor, "quality_monitor"))
    builder.add_node("generate_report", wrap_node_with_trace(generate_report, "generate_report"))
    builder.add_node("export_kg", wrap_node_with_trace(export_kg, "export_kg"))

    # ── Edges (same routing as build_modeling_agent_graph) ──
    builder.set_entry_point("receive_task")
    builder.add_edge("receive_task", "schema_explore_light")  # Skip blueprint_match (needs PG)
    builder.add_conditional_edges("schema_explore_light", route_after_schema_explore)
    builder.add_conditional_edges("detect_sensitive_fields", route_after_sensitive_fields)
    builder.add_edge("confirm_sensitive", "human_confirm_deep")
    builder.add_conditional_edges("human_confirm_deep", route_after_deep_explore)
    builder.add_edge("deep_explore", "grill_clarifications")
    builder.add_conditional_edges("grill_clarifications", route_after_grill)
    builder.add_conditional_edges("wait_human", route_after_human_response)
    builder.add_conditional_edges("process_approval", route_after_approval)
    builder.add_conditional_edges("detect_schema_drifts", route_after_schema_drift)
    builder.add_conditional_edges("generate_drift_fixes", route_after_drift_fix)
    builder.add_conditional_edges("design_ods", route_after_ods_design)
    builder.add_conditional_edges("design_dim", route_after_dim_design)
    builder.add_conditional_edges("design_dwd", route_after_dwd_design)
    builder.add_edge("design_dws", "design_ads")
    builder.add_conditional_edges("design_ads", route_after_design)
    builder.add_conditional_edges("harness_validate", route_after_validation)
    builder.add_edge("human_approval", "process_approval")
    builder.add_edge("design_revise", "harness_validate")
    builder.add_conditional_edges("dag_deploy", route_after_deploy)
    builder.add_conditional_edges("execute_tasks", route_after_execution)
    builder.add_conditional_edges("classify_errors", route_after_error_classify)
    builder.add_edge("retry_execution", "execute_tasks")
    builder.add_conditional_edges("final_validate", route_after_final_validate)
    builder.add_conditional_edges("error_handling", route_after_error_handling)
    builder.add_conditional_edges("quality_monitor", route_after_quality_monitor)
    builder.add_conditional_edges("generate_report", route_after_report)
    builder.add_edge("export_kg", END)

    return builder


# ── Node implementations (imported from dedicated modules) ──

from wren_agent.tools.error_recovery import wrap_node_with_recovery

from wren_agent.nodes.receive_task import receive_task
from wren_agent.nodes.blueprint_match import blueprint_match
from wren_agent.nodes.explore import (
    confirm_sensitive,
    deep_explore,
    detect_sensitive_fields,
    human_confirm_deep,
    schema_explore_light,
)
from wren_agent.nodes.grill import (
    grill_clarifications,
    wait_human,
)
from wren_agent.nodes.deployment import (
    classify_errors,
    dag_deploy,
    detect_schema_drifts,
    error_handling,
    execute_tasks,
    generate_drift_fixes,
    retry_execution,
)
from wren_agent.nodes.design_layers import (
    design_ads,
    design_dim,
    design_dwd,
    design_dws,
    design_ods,
)
from wren_agent.nodes.validation import (
    design_revise,
    final_validate,
    harness_validate,
)
from wren_agent.nodes.approval import (
    human_approval,
    process_approval,
)
from wren_agent.nodes.quality_monitor import quality_monitor
from wren_agent.nodes.reporting import (
    export_kg,
    generate_report,
)

# ── Apply recovery + trace wrappers on nodes ─────────────────────
# Recovery: per-node config inferred from service dependency.
#   skip-on-failure: LLM, Neo4j (non-critical)
#   escalate-on-failure: DS, SourceDB, Doris (critical)
# Trace (outermost): current_node + node_timeline accumulation, WS events,
#   and incremental PG persistence so the dashboard survives page switches.

from wren_agent.tools.node_tracer import wrap_node_with_trace as _t

_receive_task          = _t(receive_task, "receive_task")
_blueprint_match       = _t(blueprint_match, "blueprint_match")

_schema_explore_light  = _t(wrap_node_with_recovery(schema_explore_light, "schema_explore_light"), "schema_explore_light")
_detect_sensitive_fields = _t(detect_sensitive_fields, "detect_sensitive_fields")
_confirm_sensitive     = _t(confirm_sensitive, "confirm_sensitive")
_deep_explore          = _t(wrap_node_with_recovery(deep_explore, "deep_explore"), "deep_explore")
_human_confirm_deep    = _t(human_confirm_deep, "human_confirm_deep")

_grill_clarifications  = _t(wrap_node_with_recovery(grill_clarifications, "grill_clarifications"), "grill_clarifications")
_wait_human            = _t(wait_human, "wait_human")

_detect_schema_drifts  = _t(wrap_node_with_recovery(detect_schema_drifts, "detect_schema_drifts"), "detect_schema_drifts")
_generate_drift_fixes  = _t(generate_drift_fixes, "generate_drift_fixes")

_design_ods            = _t(wrap_node_with_recovery(design_ods, "design_ods"), "design_ods")
_design_dim            = _t(wrap_node_with_recovery(design_dim, "design_dim"), "design_dim")
_design_dwd            = _t(wrap_node_with_recovery(design_dwd, "design_dwd"), "design_dwd")
_design_dws            = _t(wrap_node_with_recovery(design_dws, "design_dws"), "design_dws")
_design_ads            = _t(wrap_node_with_recovery(design_ads, "design_ads"), "design_ads")

_harness_validate      = _t(wrap_node_with_recovery(harness_validate, "harness_validate"), "harness_validate")
_design_revise         = _t(design_revise, "design_revise")
_final_validate        = _t(wrap_node_with_recovery(final_validate, "final_validate"), "final_validate")

_human_approval        = _t(human_approval, "human_approval")
_process_approval      = _t(process_approval, "process_approval")

_dag_deploy            = _t(wrap_node_with_recovery(dag_deploy, "dag_deploy"), "dag_deploy")
_execute_tasks         = _t(wrap_node_with_recovery(execute_tasks, "execute_tasks"), "execute_tasks")
_classify_errors       = _t(classify_errors, "classify_errors")
_retry_execution       = _t(retry_execution, "retry_execution")
_error_handling        = _t(error_handling, "error_handling")

_quality_monitor       = _t(wrap_node_with_recovery(quality_monitor, "quality_monitor"), "quality_monitor")
_generate_report       = _t(wrap_node_with_recovery(generate_report, "generate_report"), "generate_report")
_export_kg             = _t(wrap_node_with_recovery(export_kg, "export_kg"), "export_kg")
