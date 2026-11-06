-- Migration 002: Task artifacts persistence tables
-- Persists completed task artifacts for fast API reads
-- (running tasks still use LangGraph checkpointer snapshots)

CREATE TABLE IF NOT EXISTS task_artifacts (
    task_id VARCHAR(36) PRIMARY KEY,
    design_plan JSONB,
    construction_report TEXT,
    data_asset_map TEXT,
    kg_export_id VARCHAR(255),
    explorations JSONB,
    sensitive_fields JSONB,
    schema_drifts JSONB,
    incremental_strategies JSONB,
    matched_blueprints JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS task_lifecycle (
    task_id VARCHAR(36) PRIMARY KEY,
    validation_results JSONB,
    execution_results JSONB,
    error_classifications JSONB,
    clarifications JSONB,
    approval_items JSONB,
    node_timeline JSONB,
    audit_log JSONB,
    errors JSONB,
    warnings JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for listing completed tasks by recency
CREATE INDEX IF NOT EXISTS idx_task_artifacts_created
    ON task_artifacts (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_lifecycle_updated
    ON task_lifecycle (updated_at DESC);
