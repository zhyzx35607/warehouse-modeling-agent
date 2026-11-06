-- 018: Independently recoverable modeling stages and immutable artifacts.
-- The existing modeling_tasks row remains the user-visible parent task.

CREATE TABLE IF NOT EXISTS modeling_stages (
    task_id TEXT NOT NULL REFERENCES modeling_tasks(task_id) ON DELETE CASCADE,
    stage VARCHAR(32) NOT NULL CHECK (stage IN ('exploration', 'ods', 'core_model')),
    thread_id TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'partial_success', 'succeeded', 'failed', 'blocked')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    input_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    quality_gate JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, stage),
    UNIQUE (thread_id)
);

CREATE TABLE IF NOT EXISTS stage_artifacts (
    task_id TEXT NOT NULL,
    stage VARCHAR(32) NOT NULL,
    artifact_type VARCHAR(32) NOT NULL
        CHECK (artifact_type IN ('source_catalog', 'ods_manifest', 'core_model_plan')),
    version INTEGER NOT NULL CHECK (version > 0),
    payload JSONB NOT NULL,
    schema_fingerprint TEXT NOT NULL DEFAULT '',
    supersedes_version INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, stage, artifact_type, version),
    FOREIGN KEY (task_id, stage) REFERENCES modeling_stages(task_id, stage) ON DELETE CASCADE,
    CHECK ((version = 1 AND supersedes_version IS NULL)
        OR (version > 1 AND supersedes_version = version - 1))
);

CREATE INDEX IF NOT EXISTS idx_modeling_stages_status
    ON modeling_stages(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_stage_artifacts_latest
    ON stage_artifacts(task_id, stage, artifact_type, version DESC);

COMMENT ON TABLE modeling_stages IS
    'Child stage lifecycle for business-source exploration, ODS, and core modeling.';
COMMENT ON TABLE stage_artifacts IS
    'Immutable, versioned contracts exchanged between modeling stages.';
