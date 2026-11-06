"""Blueprint models for reusable modeling patterns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class BlueprintLayer(BaseModel):
    """A single layer definition within a blueprint."""
    layer: str  # ODS | DIM | DWD | DWS | ADS
    table_patterns: list[dict] = Field(default_factory=list)  # Generic table structures
    sql_templates: list[str] = Field(default_factory=list)
    dag_pattern: Optional[dict] = None  # DAG topology for this layer


class Blueprint(BaseModel):
    """A reusable, versioned modeling blueprint."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    domain: str  # e.g. 电商交易域 | 用户域 | 营销域
    version: int = 1
    layers: list[BlueprintLayer] = Field(default_factory=list)
    source_tables_signature: dict[str, Any] = Field(default_factory=dict)  # Key table patterns to match
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    source_task_id: Optional[str] = None  # The ModelingTask that produced this blueprint
    usage_count: int = 0


class BlueprintMatch(BaseModel):
    """Result of matching a ModelingTask against the Blueprint Library."""
    blueprint_id: str
    blueprint_name: str
    score: float  # 0-1 similarity score
    matched_tables: list[str] = Field(default_factory=list)
    unmatched_tables: list[str] = Field(default_factory=list)
    adaptations_needed: list[str] = Field(default_factory=list)
    selected: bool = False  # Whether human chose this blueprint
    description: str = ""  # Blueprint intent, injected into design prompts
    layers: list[dict] = Field(default_factory=list)  # Raw BlueprintLayer dicts (design content)
