"""LangGraph AgentState — the central state object for each ModelingTask."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from wren_agent.models.blueprint import Blueprint, BlueprintMatch
from wren_agent.models.design import DWDesignPlan
from wren_agent.models.schema import SchemaExploration, SchemaSnapshot
from wren_agent.models.task import (
    ApprovalItem,
    ClarificationItem,
    ErrorClassification,
    ExecutionResult,
    IncrementalStrategy,
    SchemaDriftResult,
    SensorResult,
    SourceDatabase,
    TaskPhase,
    TaskStatus,
    ValidationResult,
)


class AgentState(TypedDict, total=False):
    """The full state of a ModelingTask, persisted via LangGraph Checkpointer."""

    # ── Task identity ──
    task_id: str
    thread_id: str
    status: TaskStatus
    phase: TaskPhase
    modeling_goal: str
    mode: str  # "manual" | "auto" — task-level HITL mode (see TaskMode)
    created_at: datetime
    updated_at: datetime

    # ── Human interaction ──
    messages: Annotated[list, add_messages]  # LangGraph conversation messages
    clarifications: list[ClarificationItem]  # Pending/answered clarifications
    approval_items: list[ApprovalItem]       # Pending/resolved approvals
    pending_human_input: bool                # True = waiting for human
    human_input_type: str                    # clarification | approval | deep_explore_confirm
    approval_context: str                    # schema_drift | design_plan | execution
    auto_approval_rounds: int                # Auto mode: rounds of auto-approved execution errors (loop guard)
    error_recovery_rounds: int               # error_handling visits (loop guard, fails task at 3)

    # ── Source databases ──
    source_databases: list[SourceDatabase]
    dw_target: dict[str, Any]  # Doris target connection info (host/port/db)
    warehouse_source_id: str  # Registered Doris source receiving ODS/DIM/DWD assets
    landing: dict[str, str]  # Business mode physical databases for ods/dim/dwd
    create_missing_databases: bool
    source_mode: str  # "business_db" | "warehouse" — exploration mode (see T-07 wayfinder)
    business_task_type: str  # full | ods_migration | core_model
    insufficient_gaps: list[dict]  # Warehouse-mode gaps (缺表/缺列/粒度不匹配) from partial support

    # ── Schema exploration ──
    explorations: dict[str, SchemaExploration]  # source_db_id -> exploration
    field_semantics: dict[str, dict[str, dict]]  # table -> field -> semantics row
    _schema_prompt_cache: str  # Task-scoped trimmed explorations for design prompts
    _field_semantics_prompt: str  # Task-scoped formatted field-semantics block
    sensitive_fields: list[SensorResult]
    deep_explore_pending: list[dict]  # Items needing human confirmation for deep explore
    deep_explore_approval_id: str  # Deterministic approval card ID for recovery
    deep_explore_decision: str  # "" | approved | rejected | timeout | skipped_auto

    # ── Schema snapshots (for drift detection) ──
    prior_snapshots: dict[str, SchemaSnapshot]  # table_name -> snapshot from last run
    current_snapshots: dict[str, SchemaSnapshot]  # table_name -> current snapshot
    schema_drifts: list[SchemaDriftResult]

    # ── Blueprint matching ──
    matched_blueprints: list[BlueprintMatch]
    selected_blueprint: Optional[str]  # blueprint_id

    # ── Incremental strategy ──
    incremental_strategies: list[IncrementalStrategy]

    # ── Design artifacts ──
    design_plan: Optional[DWDesignPlan]  # The complete DW design
    compiler_failures: list[dict]  # closed-world compile refuses (layer/check/detail)
    compiler_extra_functions: list[str]  # task-scoped EXPLAIN-admitted scalars (ADR-0018)

    # ── Validation ──
    validation_results: list[ValidationResult]
    all_validations_passed: bool
    validation_blocking: bool          # Critical design defects cannot be manually approved
    validation_error_count: int        # Failed validation checks (any count blocks approval)
    design_review_issues: list[dict]   # Structured LLM review issues (severity/rule/detail)
    design_revision_count: int          # design_revise rounds so far (loop guard)
    dml_repair_tried: dict              # table → repair-doris-dml labels already tried
    dml_repair_pending: str             # last candidate label awaiting execute outcome
    last_validation_failure_signature: str  # failure fingerprint of the previous validate round (no-progress detection)
    _sandbox_validation_count: int      # sandbox validation calls so far (step-budget guard)

    # ── Deployment & execution ──
    ds_project_name: str                # Dolphin Scheduler project
    ds_project_code: Optional[int]      # Dolphin Scheduler project code (needed by execute_tasks)
    ds_data_sources: list[dict]         # Created DS data sources
    ds_dags: list[str]                  # Created DAG IDs
    deploy_failure_count: int           # Consecutive dag_deploy failures (loop guard)
    deploy_succeeded: bool              # Last dag_deploy attempt succeeded
    execution_results: list[ExecutionResult]
    error_classifications: list[ErrorClassification]
    retry_count: int                    # Overall retry counter
    max_retries: int                    # Configurable max retries

    # ── Final outputs ──
    construction_report: Optional[str]
    plain_report: Optional[str]          # Business-friendly companion report
    quality_checks: dict[str, Any]
    kg_export_id: Optional[str]         # Neo4j graph ID after export
    data_asset_map: Optional[str]       # Generated data asset map

    # ── Error tracking ──
    errors: list[str]                   # Non-recoverable error messages
    warnings: list[str]                 # Non-blocking warnings
    failure_kind: str                   # "" | validation | crash

    # ── Audit ──
    audit_log: list[dict]               # All SQLs, API calls, decisions

    # ── Observability ──
    current_node: str                   # Currently executing node name (for real-time UI)
    node_timeline: list[dict]           # [{node, status, duration_ms, timestamp, error?}]

    # ── Crash recovery ──
    recovery_count: int                 # Number of crash-recovery attempts (0 = fresh run)


# ── Initial state factory ──


def create_initial_state(
    task_id: str,
    modeling_goal: str,
    source_databases: list[SourceDatabase],
    dw_target: dict[str, Any],
    thread_id: Optional[str] = None,
    mode: str = "manual",
    source_mode: str = "business_db",
    business_task_type: str = "full",
    warehouse_source_id: str = "",
    landing: Optional[dict[str, str]] = None,
    create_missing_databases: bool = False,
) -> AgentState:
    """Create the initial state for a new ModelingTask."""
    return AgentState(
        task_id=task_id,
        thread_id=thread_id or task_id,
        status=TaskStatus.PENDING,
        phase=TaskPhase.INIT,
        modeling_goal=modeling_goal,
        mode=mode,
        source_mode=source_mode,
        business_task_type=business_task_type,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        messages=[],
        clarifications=[],
        approval_items=[],
        pending_human_input=False,
        human_input_type="",
        approval_context="",
        auto_approval_rounds=0,
        error_recovery_rounds=0,
        source_databases=source_databases,
        dw_target=dw_target,
        warehouse_source_id=warehouse_source_id,
        landing=landing or {},
        create_missing_databases=create_missing_databases,
        insufficient_gaps=[],
        explorations={},
        field_semantics={},
        _schema_prompt_cache="",
        _field_semantics_prompt="",
        sensitive_fields=[],
        deep_explore_pending=[],
        deep_explore_approval_id="",
        deep_explore_decision="",
        prior_snapshots={},
        current_snapshots={},
        schema_drifts=[],
        matched_blueprints=[],
        selected_blueprint=None,
        incremental_strategies=[],
        design_plan=None,
        compiled_semantics={},
        compiler_failures=[],
        compiler_extra_functions=[],
        validation_results=[],
        all_validations_passed=False,
        validation_blocking=False,
        validation_error_count=0,
        design_review_issues=[],
        design_revision_count=0,
        dml_repair_tried={},
        dml_repair_pending="",
        last_validation_failure_signature="",
        _sandbox_validation_count=0,
        ds_project_name="",
        ds_project_code=None,
        ds_data_sources=[],
        ds_dags=[],
        deploy_failure_count=0,
        deploy_succeeded=False,
        execution_results=[],
        error_classifications=[],
        retry_count=0,
        max_retries=3,
        construction_report=None,
        plain_report=None,
        quality_checks={},
        kg_export_id=None,
        data_asset_map=None,
        errors=[],
        warnings=[],
        failure_kind="",
        audit_log=[],
        current_node="",
        node_timeline=[],
        recovery_count=0,
    )


def is_auto_mode(state: AgentState) -> bool:
    """True when the task runs unattended (auto mode).

    Older states/checkpoints have no `mode` key — treated as manual.
    """
    return state.get("mode", "manual") == "auto"


def is_ods_migration_task(state: AgentState) -> bool:
    """Whole-database ODS 1:1 sync, including checkpoints created as `full`
    with the dashboard's 整库迁移 goal template (task 9fcfcdbb)."""
    if state.get("business_task_type") == "ods_migration":
        return True
    goal = state.get("modeling_goal") or ""
    return "全部业务表" in goal and "一对一迁移" in goal
