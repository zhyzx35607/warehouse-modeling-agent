"""Contracts for independently recoverable modeling stages."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ModelingStage(str, Enum):
    EXPLORATION = "exploration"
    ODS = "ods"
    CORE_MODEL = "core_model"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL_SUCCESS = "partial_success"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class StageArtifactType(str, Enum):
    SOURCE_CATALOG = "source_catalog"
    ODS_MANIFEST = "ods_manifest"
    CORE_MODEL_PLAN = "core_model_plan"


class CatalogColumn(BaseModel):
    """Stable source-column contract; profiling values are intentionally absent."""
    name: str
    data_type: str
    ordinal: int = Field(ge=0)
    nullable: bool = True
    primary_key: bool = False
    incremental_key: bool = False
    source_column_id: str = ""
    comment: str = ""


class CatalogTable(BaseModel):
    source_db_id: str
    schema_name: str
    table_name: str
    columns: list[CatalogColumn] = Field(default_factory=list)
    source_table_id: str = ""
    required: bool = False

    @property
    def identity(self) -> str:
        return self.source_table_id or f"{self.source_db_id}:{self.schema_name}.{self.table_name}"


class SourceCatalog(BaseModel):
    tables: list[CatalogTable] = Field(default_factory=list)
    schema_fingerprint: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CatalogChangeKind(str, Enum):
    TABLE_ADDED = "table_added"
    TABLE_REMOVED = "table_removed"
    COLUMN_ADDED = "column_added"
    COLUMN_REMOVED = "column_removed"
    COLUMN_RENAMED = "column_renamed"
    TYPE_WIDENED = "type_widened"
    TYPE_NARROWED = "type_narrowed"
    KEY_CHANGED = "key_changed"
    INCREMENTAL_KEY_CHANGED = "incremental_key_changed"
    NULLABILITY_TIGHTENED = "nullability_tightened"
    COMMENT_CHANGED = "comment_changed"


class CatalogChange(BaseModel):
    kind: CatalogChangeKind
    table_identity: str
    column: str = ""
    previous: Any = None
    current: Any = None
    invalidates_ods: bool = False


class CatalogDiff(BaseModel):
    compatible: bool = True
    changes: list[CatalogChange] = Field(default_factory=list)
    invalidated_tables: list[str] = Field(default_factory=list)


class ODSSyncMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


class ODSTableStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ODSTableManifest(BaseModel):
    table_identity: str
    source_db_id: str
    source_table: str
    ods_table: str
    required: bool = False
    initial_mode: ODSSyncMode = ODSSyncMode.FULL
    steady_mode: ODSSyncMode = ODSSyncMode.FULL
    incremental_column: str = ""
    sync_method: str = "datax"
    source_catalog_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_incremental_mode(self) -> "ODSTableManifest":
        if self.steady_mode == ODSSyncMode.INCREMENTAL and not self.incremental_column:
            raise ValueError("incremental steady mode requires incremental_column")
        return self


class ODSManifest(BaseModel):
    source_catalog_version: int = Field(ge=1)
    tables: list[ODSTableManifest] = Field(default_factory=list)


class ODSTableRun(BaseModel):
    task_id: str
    manifest_version: int = Field(ge=1)
    table_identity: str
    attempt: int = Field(ge=1)
    required: bool = False
    sync_mode: ODSSyncMode
    status: ODSTableStatus
    source_rows: int | None = Field(default=None, ge=0)
    target_rows: int | None = Field(default=None, ge=0)
    watermark_before: str = ""
    watermark_after: str = ""
    watermark_monotonic: bool | None = None
    watermark_committed: bool = False
    error_type: str = ""
    error: str = ""


class ODSQualityGate(BaseModel):
    status: StageStatus
    passed_tables: list[str] = Field(default_factory=list)
    failed_required_tables: list[str] = Field(default_factory=list)
    failed_optional_tables: list[str] = Field(default_factory=list)
    retryable_tables: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)


class ModelingStageRun(BaseModel):
    task_id: str
    stage: ModelingStage
    thread_id: str
    status: StageStatus = StageStatus.PENDING
    attempt: int = 0
    input_versions: dict[str, int] = Field(default_factory=dict)
    quality_gate: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_terminal_timestamp(self) -> "ModelingStageRun":
        terminal = {StageStatus.PARTIAL_SUCCESS, StageStatus.SUCCEEDED, StageStatus.FAILED}
        if self.status in terminal and self.finished_at is None:
            raise ValueError("terminal stage status requires finished_at")
        return self


class StageArtifact(BaseModel):
    task_id: str
    stage: ModelingStage
    artifact_type: StageArtifactType
    version: int = Field(ge=1)
    payload: dict[str, Any]
    schema_fingerprint: str = ""
    supersedes_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_version_chain(self) -> "StageArtifact":
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("version 1 cannot supersede another version")
        if self.version > 1 and self.supersedes_version != self.version - 1:
            raise ValueError("artifact must supersede the immediately previous version")
        return self
