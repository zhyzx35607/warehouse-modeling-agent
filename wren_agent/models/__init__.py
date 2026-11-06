"""Domain models for the Wren modeling agent."""

from wren_agent.models.task import (
    TaskStatus,
    TaskPhase,
    ClarificationItem,
    ClarificationStatus,
    ApprovalItem,
    ApprovalStatus,
    SourceDatabase,
    SensorType,
    SensorResult,
    ErrorClassification,
    ErrorType,
    ExecutionResult,
    IncrementalStrategy,
    SchemaDriftResult,
    DriftType,
    ValidationLevel,
    ValidationResult,
)

from wren_agent.models.blueprint import (
    Blueprint,
    BlueprintLayer,
    BlueprintMatch,
)

from wren_agent.models.schema import (
    TableSchema,
    FieldSchema,
    TableRelationship,
    RelationshipType,
    SchemaExploration,
    SchemaSnapshot,
)

from wren_agent.models.design import (
    ODSDesign,
    DIMDesign,
    DWDDesign,
    DWSDesign,
    ADSDesign,
    DWDesignPlan,
    DataXConfig,
    FlinkCDCConfig,
    SQLArtifact,
)
from wren_agent.models.stage import (
    CatalogChange,
    CatalogChangeKind,
    CatalogColumn,
    CatalogDiff,
    CatalogTable,
    ModelingStage,
    ODSManifest,
    ODSQualityGate,
    ODSSyncMode,
    ODSTableManifest,
    ODSTableRun,
    ODSTableStatus,
    SourceCatalog,
    StageArtifact,
    StageArtifactType,
    ModelingStageRun,
    StageStatus,
)

__all__ = [
    # Task
    "TaskStatus", "TaskPhase", "ClarificationItem", "ClarificationStatus",
    "ApprovalItem", "ApprovalStatus", "SourceDatabase", "SensorType",
    "SensorResult", "ErrorClassification", "ErrorType", "ExecutionResult",
    "IncrementalStrategy", "SchemaDriftResult", "DriftType",
    "ValidationLevel", "ValidationResult",
    # Blueprint
    "Blueprint", "BlueprintLayer", "BlueprintMatch",
    # Schema
    "TableSchema", "FieldSchema", "TableRelationship", "RelationshipType",
    "SchemaExploration", "SchemaSnapshot",
    # Design
    "ODSDesign", "DIMDesign", "DWDDesign", "DWSDesign", "ADSDesign",
    "DWDesignPlan", "DataXConfig", "FlinkCDCConfig", "SQLArtifact",
    # Stage orchestration
    "ModelingStage", "StageArtifact", "StageArtifactType",
    "ModelingStageRun", "StageStatus",
    "CatalogColumn", "CatalogTable", "SourceCatalog", "CatalogChangeKind",
    "CatalogChange", "CatalogDiff",
    "ODSSyncMode", "ODSTableStatus", "ODSTableManifest", "ODSManifest",
    "ODSTableRun", "ODSQualityGate",
]
