"""Data warehouse design artifact models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SQLArtifact(BaseModel):
    """A SQL artifact (DDL, DML, or query) for a specific layer."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    layer: str  # ODS | DIM | DWD | DWS | ADS
    target_table: str
    sql_type: str  # DDL | DML_INSERT | DML_SELECT | DML_MERGE
    sql: str
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)  # upstream table names
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DataXConfig(BaseModel):
    """DataX JSON configuration for offline sync."""
    target_table: str
    source_db_type: str
    source_query: str  # Or table name
    doris_table: str
    doris_columns: list[str] = Field(default_factory=list)
    doris_load_props: dict = Field(default_factory=dict)
    json_config: str = ""  # Full DataX JSON
    incremental_where: Optional[str] = None  # WHERE clause for incremental sync


class FlinkCDCConfig(BaseModel):
    """Flink CDC configuration for real-time sync."""
    target_table: str
    source_db_type: str
    source_table: str
    source_host: str
    source_port: int
    source_database: str
    doris_table: str
    server_id: Optional[str] = None  # MySQL binlog server id
    table_list: list[str] = Field(default_factory=list)
    startup_mode: str = "initial"  # initial | latest-offset | timestamp


# ── Layer Designs ────────────────────────────────────────────────


class ODSDesign(BaseModel):
    """ODS layer design - one entry per source table."""
    source_table: str
    source_db_id: str
    ods_table: str
    fields: list[dict] = Field(default_factory=list)
    sync_method: str = "datax"  # datax | flink_cdc
    datax_config: Optional[DataXConfig] = None
    flink_cdc_config: Optional[FlinkCDCConfig] = None
    incremental_strategy: str = "full"  # full | incremental | incremental_with_full_reconcile
    incremental_column: Optional[str] = None
    schedule_frequency: str = "daily"


class DIMDesign(BaseModel):
    """DIM layer design for a dimension table."""
    dim_name: str
    dim_table: str
    source: str  # "from_source_table" | "from_existing_dim" | "manual_definition"
    source_table: Optional[str] = None
    existing_dim_ref: Optional[str] = None  # reference to existing DIM in registry
    fields: list[dict] = Field(default_factory=list)
    natural_key: list[str] = Field(default_factory=list)
    surrogate_key: Optional[str] = None
    dim_variant_of: Optional[str] = None  # parent dim name if this is a variant
    version: int = 1
    registered: bool = False  # Whether registered in DimensionRegistry


class DWDDesign(BaseModel):
    """DWD layer design for a fact table."""
    fact_name: str
    fact_table: str
    business_process: str = ""
    source_ods_tables: list[str] = Field(default_factory=list)
    fields: list[dict] = Field(default_factory=list)
    measures: list[dict] = Field(default_factory=list)  # {name, expression, type}
    dimension_refs: list[str] = Field(default_factory=list)  # Referenced dim tables
    grain: list[str] = Field(default_factory=list)  # The grain of this fact table


class DWSDesign(BaseModel):
    """DWS layer design for an aggregated summary table."""
    summary_name: str
    summary_table: str
    source_dwd_table: str
    # Warehouse mode: multiple existing fact tables can be combined into one
    # summary (e.g. 成交 DWD + 退款 DWD → 净成交) — see wayfinder T-08.
    source_tables: list[str] = Field(default_factory=list)
    fields: list[dict] = Field(default_factory=list)
    measures: list[dict] = Field(default_factory=list)
    group_by_dimensions: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)  # Must match across all measures
    requirement_coverage: list[dict] = Field(default_factory=list)
    refresh_logic: str = "incremental"  # incremental | full_refresh


class ADSDesign(BaseModel):
    """ADS layer design for application-specific metrics."""
    application_name: str
    ads_table: str
    description: str = ""
    source_dws_tables: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)  # Physical query grain columns
    fields: list[dict] = Field(default_factory=list)
    metrics: list[dict] = Field(default_factory=list)  # {name, expression, metric_registry_id}
    requirement_coverage: list[dict] = Field(default_factory=list)
    target_audience: str = ""  # BI report name, dashboard, etc.


class DWDesignPlan(BaseModel):
    """Complete data warehouse design plan."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    modeling_goal: str
    ods_designs: list[ODSDesign] = Field(default_factory=list)
    dim_designs: list[DIMDesign] = Field(default_factory=list)
    dwd_designs: list[DWDDesign] = Field(default_factory=list)
    dws_designs: list[DWSDesign] = Field(default_factory=list)
    ads_designs: list[ADSDesign] = Field(default_factory=list)
    sql_artifacts: list[SQLArtifact] = Field(default_factory=list)
    datax_configs: list[DataXConfig] = Field(default_factory=list)
    flink_cdc_configs: list[FlinkCDCConfig] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    approved: bool = False
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
