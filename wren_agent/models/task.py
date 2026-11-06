"""Core task models for the Wren modeling agent."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Task lifecycle ──────────────────────────────────────────────


# Task-level HITL mode: manual = pause at human nodes; auto = unattended (agent
# answers clarifications with the LLM recommendation and auto-approves).
TaskMode = Literal["manual", "auto"]


class TaskStatus(str, Enum):
    PENDING = "pending"             # 任务已注册，等待启动
    EXPLORING = "exploring"         # 探索业务数据库 schema
    GRILLING = "grilling"           # 拷问人类专家
    DESIGNING = "designing"         # 设计分层数仓方案
    VALIDATING = "validating"       # Harness 校验层执行中
    AWAITING_APPROVAL = "awaiting_approval"  # 等待人类审批
    DEPLOYING = "deploying"         # 部署 DAG 到 Dolphin Scheduler
    EXECUTING = "executing"         # 执行 DAG 任务
    COMPLETED = "completed"         # 成功完成
    FAILED = "failed"               # 失败（不可恢复）
    CANCELLED = "cancelled"         # 被取消


class TaskPhase(str, Enum):
    """Fine-grained phase within the task lifecycle."""
    INIT = "init"
    BLUEPRINT_MATCH = "blueprint_match"
    SCHEMA_EXPLORE_LIGHT = "schema_explore_light"
    GRILL_CLARIFICATIONS = "grill_clarifications"
    SCHEMA_EXPLORE_DEEP = "schema_explore_deep"
    DESIGN_ODS = "design_ods"
    DESIGN_DIM = "design_dim"
    DESIGN_DWD = "design_dwd"
    DESIGN_DWS = "design_dws"
    DESIGN_ADS = "design_ads"
    HARNESS_VALIDATE = "harness_validate"
    HUMAN_APPROVAL = "human_approval"
    DAG_DEPLOY = "dag_deploy"
    EXECUTE = "execute"
    ERROR_HANDLING = "error_handling"
    FINAL_VALIDATE = "final_validate"
    REPORT = "report"
    KG_EXPORT = "kg_export"


# ── Human interaction ───────────────────────────────────────────


class ClarificationStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    ESCALATED = "escalated"
    SKIPPED = "skipped"


class ClarificationItem(BaseModel):
    """A question Agent asks the human expert."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    context: str = ""  # Why this question matters
    candidates: list[str] = Field(default_factory=list)  # Suggested answers
    recommended: Optional[str] = None  # LLM recommended answer (verbatim from candidates when they exist; None = rule-based)
    recommended_reason: str = ""       # One-line justification for the recommendation
    impact_level: str = "medium"  # low | medium | high | critical
    blocking: bool = True  # True = pause task until answered
    status: ClarificationStatus = ClarificationStatus.PENDING
    answer: Optional[str] = None
    answered_at: Optional[datetime] = None
    clarification_type: str = ""  # metric_definition | table_relation | business_logic | data_quality


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalItem(BaseModel):
    """An item requiring human approval."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str
    artifact_type: str  # design_plan | data_source | execution | drifty_fix
    artifact_id: Optional[str] = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    comment: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None


# ── Schema exploration ──────────────────────────────────────────


class SensorType(str, Enum):
    """Type of sensitive field detected during exploration."""
    PHONE = "phone"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    EMAIL = "email"
    ADDRESS = "address"
    NAME = "name"
    PASSWORD = "password"
    IP = "ip"
    UNKNOWN = "unknown"


class SensorResult(BaseModel):
    """Result of sensitive field detection."""
    table_name: str
    field_name: str
    sensor_type: SensorType
    sample_value: Optional[str] = None
    confidence: float = 0.0  # 0-1
    human_confirmed: bool = False
    mask_strategy: Optional[str] = None  # null_out | hash | partial_mask | pass_through


class SourceDatabase(BaseModel):
    """Metadata about a connected source database."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    db_type: str  # mysql | postgresql | oracle | sqlserver
    host: str
    port: int
    database: str
    mcp_server_name: str  # MCP server instance name
    access_mode: str = "readonly"  # readonly | readwrite
    tables: list[str] = Field(default_factory=list)
    explored: bool = False
    # Driver-level options, e.g. {"use_ssl": false} — merged with the
    # registry's connection_params during exploration
    connection_params: dict = Field(default_factory=dict)
    # Warehouse mode only: layer -> database mapping for a Doris cluster
    # (e.g. {"ods": "db_ods", "dim": "wren_dw", ...}); one database may
    # host multiple layers (wren_dw) and one layer may host multiple
    # databases (auto-discovered business domains) — see wayfinder T-01/T-02
    layers: dict[str, Any] = Field(default_factory=dict)


# ── Incremental strategy ────────────────────────────────────────


class IncrementalStrategy(BaseModel):
    """Agent-recommended incremental strategy for a source table."""
    table_name: str
    supports_incremental: bool = False
    incremental_column: Optional[str] = None  # e.g. updated_at
    incremental_type: str = "full"  # full | incremental | incremental_with_full_reconcile
    recommended_frequency: str = "daily"  # hourly | daily | weekly
    reason: str = ""
    human_overridden: bool = False
    human_decision: Optional[str] = None


# ── Error handling ──────────────────────────────────────────────


class ErrorType(str, Enum):
    TRANSIENT = "transient"       # 网络抖动、连接池耗尽、锁超时
    RESOURCE = "resource"         # Doris 磁盘满、内存不足
    DATA = "data"                 # 源库脏数据、约束冲突
    LOGIC = "logic"               # SQL 逻辑错误
    CONFIG = "config"             # 数据源配置错误、权限不足
    DEPENDENCY = "dependency"     # 上游表 schema 变更


class ErrorClassification(BaseModel):
    """Classified error from Dolphin Scheduler task failure."""
    task_instance_id: str
    error_type: ErrorType
    raw_log: str
    summary: str
    retry_count: int = 0
    max_retries: int = 3  # 只有 TRANSIENT 才重试
    action: str = "escalate"  # retry | escalate | skip | wait
    details: str = ""  # LLM 生成的详细解释（关键词分类为空）
    fix_suggestion: str = ""  # LLM 生成的修复建议（关键词分类为空）
    llm_classified: bool = False  # True 表示该分类由 LLM 产出


class ExecutionResult(BaseModel):
    """Result of a DAG execution attempt."""
    task_instance_id: str
    success: bool
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: Optional[ErrorClassification] = None
    logs: list[str] = Field(default_factory=list)
    rows_written: dict[str, int] = Field(default_factory=dict)


# ── Schema drift ────────────────────────────────────────────────


class DriftType(str, Enum):
    COLUMN_REMOVED = "column_removed"         # Breaking
    COLUMN_TYPE_CHANGED = "column_type_changed"  # Breaking
    COLUMN_RENAMED = "column_renamed"         # Breaking (agent may auto-detect semantic match)
    COLUMN_ADDED = "column_added"             # Additive
    TABLE_REMOVED = "table_removed"           # Breaking
    TABLE_RENAMED = "table_renamed"           # Breaking
    NO_CHANGE = "no_change"


class SchemaDriftResult(BaseModel):
    """Result of schema snapshot comparison."""
    table_name: str
    drift_type: DriftType
    details: str
    is_breaking: bool = False
    previous_snapshot: Optional[dict] = None
    current_snapshot: Optional[dict] = None
    auto_fix_sql: Optional[str] = None  # Agent-generated repair SQL
    human_approved_fix: bool = False


# ── Validation ──────────────────────────────────────────────────


class ValidationLevel(str, Enum):
    STRUCTURE = "structure"       # SQL 语法正确、表/字段存在
    CONSISTENCY = "consistency"   # 行数对账、聚合加总一致
    SEMANTIC = "semantic"         # 指标值与预期值对比


class ValidationResult(BaseModel):
    """Result of a validation run."""
    level: ValidationLevel
    layer: str  # ODS | DIM | DWD | DWS | ADS
    passed: Optional[bool]  # True/False = auto-verdict; None = needs human review (SEMANTIC)
    checks: list[dict] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)
    semantic_assertions: list[dict] = Field(default_factory=list)  # Only for SEMANTIC
    executed_at: datetime = Field(default_factory=datetime.utcnow)
