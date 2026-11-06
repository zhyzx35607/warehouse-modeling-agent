"""Schema exploration models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class RelationshipType(str, Enum):
    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"
    MANY_TO_MANY = "N:M"
    UNKNOWN = "unknown"


class FieldSchema(BaseModel):
    """A single database field."""
    name: str
    data_type: str  # Original DB type (e.g. VARCHAR(255))
    doris_type: Optional[str] = None  # Mapped Doris type
    nullable: bool = True
    primary_key: bool = False
    default_value: Optional[Any] = None
    comment: Optional[str] = None
    sample_values: list[Any] = Field(default_factory=list)
    distinct_count: Optional[int] = None
    null_ratio: Optional[float] = None
    is_sensitive: bool = False  # Detected during exploration
    is_incremental_key: bool = False  # Suitable as incremental column


class TableSchema(BaseModel):
    """A database table with its fields."""
    name: str
    schema_name: str = "public"
    db_type: str  # mysql | postgresql | oracle
    fields: list[FieldSchema] = Field(default_factory=list)
    row_count_estimate: Optional[int] = None  # From stats, not full COUNT
    comment: Optional[str] = None
    explored_light: bool = False
    explored_deep: bool = False

    @property
    def primary_keys(self) -> list[str]:
        return [f.name for f in self.fields if f.primary_key]

    @property
    def nullable_columns(self) -> list[str]:
        return [f.name for f in self.fields if f.nullable]

    @property
    def incremental_candidates(self) -> list[str]:
        return [f.name for f in self.fields if f.is_incremental_key]


class TableRelationship(BaseModel):
    """A relationship between two tables."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    relationship_type: RelationshipType = RelationshipType.UNKNOWN
    verified: bool = False  # Verified via actual JOIN
    verified_ratio: Optional[float] = None  # Actual cardinality ratio
    human_confirmed: bool = False  # Confirmed by human expert
    source: str = ""  # inferred | human | both


class SchemaExploration(BaseModel):
    """Complete schema exploration result for a source database."""
    source_db_id: str
    tables: list[TableSchema] = Field(default_factory=list)
    relationships: list[TableRelationship] = Field(default_factory=list)
    explored_at: datetime = Field(default_factory=datetime.utcnow)
    phase: str = "light"  # light | deep
    # Full `list_tables()` catalog captured before explore-cap / keyword
    # filtering. Drift detection uses this so an unexplored table is not
    # reported as TABLE_REMOVED.
    catalog_table_names: list[str] = Field(default_factory=list)


class SchemaSnapshot(BaseModel):
    """A snapshot of a table schema at a point in time (for drift detection)."""
    table_name: str
    source_db_id: str
    snapshot_at: datetime = Field(default_factory=datetime.utcnow)
    fields: dict[str, str] = Field(default_factory=dict)  # field_name -> data_type
    primary_keys: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)
