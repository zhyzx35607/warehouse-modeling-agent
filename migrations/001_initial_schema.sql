-- ================================================================
-- Wren Modeling Agent — PostgreSQL Migration v1
-- Creates all required tables, indexes, and extensions
-- ================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- For fuzzy text matching (similarity())

-- ================================================================
-- 1. modeling_tasks — Task metadata (complements LangGraph checkpointer)
-- ================================================================
CREATE TABLE IF NOT EXISTS modeling_tasks (
    task_id         TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending',
    phase           TEXT NOT NULL DEFAULT 'init',
    modeling_goal   TEXT NOT NULL,
    source_databases JSONB NOT NULL DEFAULT '[]',
    dw_target       JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON modeling_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created    ON modeling_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_updated    ON modeling_tasks(updated_at);

-- ================================================================
-- 2. task_audit_log — Full audit trail of all agent actions
-- ================================================================
CREATE TABLE IF NOT EXISTS task_audit_log (
    id          SERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES modeling_tasks(task_id) ON DELETE CASCADE,
    phase       TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    details     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_task     ON task_audit_log(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_phase    ON task_audit_log(phase);
CREATE INDEX IF NOT EXISTS idx_audit_event    ON task_audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_created  ON task_audit_log(created_at);

-- ================================================================
-- 3. dimension_registry — Shared DIM layer governance
-- ================================================================
CREATE TABLE IF NOT EXISTS dimension_registry (
    dim_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dim_name        TEXT NOT NULL,
    dim_table       TEXT NOT NULL,
    owner_task_id   TEXT NOT NULL,
    natural_keys    TEXT[] NOT NULL DEFAULT '{}',
    grain           TEXT NOT NULL DEFAULT '',
    fields          JSONB NOT NULL DEFAULT '[]',
    variant_of      TEXT,  -- parent dim_name if this is a variant
    version         INTEGER NOT NULL DEFAULT 1,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(dim_table, version)
);

CREATE INDEX IF NOT EXISTS idx_dim_name    ON dimension_registry(dim_name);
CREATE INDEX IF NOT EXISTS idx_dim_owner   ON dimension_registry(owner_task_id);
CREATE INDEX IF NOT EXISTS idx_dim_active  ON dimension_registry(active);

-- ================================================================
-- 4. metric_registry — Global metric definition governance
-- ================================================================
CREATE TABLE IF NOT EXISTS metric_registry (
    metric_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_name     TEXT NOT NULL,
    metric_type     TEXT NOT NULL DEFAULT 'atomic',  -- atomic | derived
    definition_sql  TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    owner_task_id   TEXT NOT NULL,
    grain           TEXT NOT NULL DEFAULT '',
    parent_metrics  TEXT[] NOT NULL DEFAULT '{}',    -- For derived metrics
    tags            TEXT[] NOT NULL DEFAULT '{}',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metric_name    ON metric_registry(metric_name);
CREATE INDEX IF NOT EXISTS idx_metric_name_sim ON metric_registry USING GIN (metric_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_metric_owner   ON metric_registry(owner_task_id);
CREATE INDEX IF NOT EXISTS idx_metric_active  ON metric_registry(active);
CREATE INDEX IF NOT EXISTS idx_metric_type    ON metric_registry(metric_type);

-- ================================================================
-- 5. blueprint_library — Reusable modeling patterns
-- ================================================================
CREATE TABLE IF NOT EXISTS blueprint_library (
    blueprint_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    domain                  TEXT NOT NULL DEFAULT '',
    layers                  JSONB NOT NULL DEFAULT '[]',
    source_tables_signature JSONB NOT NULL DEFAULT '{}',
    source_task_id          TEXT,
    version                 INTEGER NOT NULL DEFAULT 1,
    tags                    TEXT[] NOT NULL DEFAULT '{}',
    usage_count             INTEGER NOT NULL DEFAULT 0,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blueprint_domain ON blueprint_library(domain);
CREATE INDEX IF NOT EXISTS idx_blueprint_tags   ON blueprint_library USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_blueprint_usage  ON blueprint_library(usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_blueprint_active ON blueprint_library(active);

-- ================================================================
-- 6. schema_snapshots — Schema drift detection
-- ================================================================
CREATE TABLE IF NOT EXISTS schema_snapshots (
    snapshot_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         TEXT NOT NULL REFERENCES modeling_tasks(task_id) ON DELETE CASCADE,
    table_name      TEXT NOT NULL,
    source_db_id    TEXT NOT NULL,
    fields          JSONB NOT NULL DEFAULT '{}',
    primary_keys    TEXT[] NOT NULL DEFAULT '{}',
    has_drift       BOOLEAN NOT NULL DEFAULT FALSE,
    drift_details   JSONB NOT NULL DEFAULT '[]',
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snap_table    ON schema_snapshots(table_name, source_db_id, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_task     ON schema_snapshots(task_id);
CREATE INDEX IF NOT EXISTS idx_snap_drift    ON schema_snapshots(has_drift) WHERE has_drift = TRUE;

-- ================================================================
-- 7. task_clarifications — Persisted clarification items
-- ================================================================
CREATE TABLE IF NOT EXISTS task_clarifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             TEXT NOT NULL REFERENCES modeling_tasks(task_id) ON DELETE CASCADE,
    question            TEXT NOT NULL,
    context             TEXT NOT NULL DEFAULT '',
    candidates          JSONB NOT NULL DEFAULT '[]',
    impact_level        TEXT NOT NULL DEFAULT 'medium',  -- low|medium|high|critical
    blocking            BOOLEAN NOT NULL DEFAULT TRUE,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|answered|escalated|skipped
    answer              TEXT,
    answered_at         TIMESTAMPTZ,
    clarification_type  TEXT NOT NULL DEFAULT '',  -- metric_definition|table_relation|business_logic|data_quality|sensitive_field
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clar_task    ON task_clarifications(task_id);
CREATE INDEX IF NOT EXISTS idx_clar_status  ON task_clarifications(task_id, status);
CREATE INDEX IF NOT EXISTS idx_clar_block   ON task_clarifications(task_id, blocking) WHERE blocking = TRUE AND status = 'pending';

-- ================================================================
-- 8. task_approvals — Persisted approval items
-- ================================================================
CREATE TABLE IF NOT EXISTS task_approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         TEXT NOT NULL REFERENCES modeling_tasks(task_id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    artifact_type   TEXT NOT NULL,  -- design_plan|data_source|execution|drift_fix
    artifact_id     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    comment         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_approv_task   ON task_approvals(task_id);
CREATE INDEX IF NOT EXISTS idx_approv_status ON task_approvals(task_id, status);

-- ================================================================
-- Post-migration: log success
-- ================================================================
DO $$
BEGIN
    RAISE NOTICE 'Wren Agent migration v1 completed successfully. Tables: modeling_tasks, task_audit_log, dimension_registry, metric_registry, blueprint_library, schema_snapshots, task_clarifications, task_approvals';
END $$;
