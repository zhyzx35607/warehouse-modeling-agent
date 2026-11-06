-- 019: Per-table ODS execution attempts for local retry and quality gates.

CREATE TABLE IF NOT EXISTS ods_table_runs (
    task_id TEXT NOT NULL,
    manifest_version INTEGER NOT NULL CHECK (manifest_version > 0),
    table_identity TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    required BOOLEAN NOT NULL DEFAULT FALSE,
    sync_mode VARCHAR(16) NOT NULL CHECK (sync_mode IN ('full', 'incremental')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    source_rows BIGINT CHECK (source_rows >= 0),
    target_rows BIGINT CHECK (target_rows >= 0),
    watermark_before TEXT NOT NULL DEFAULT '',
    watermark_after TEXT NOT NULL DEFAULT '',
    watermark_monotonic BOOLEAN,
    watermark_committed BOOLEAN NOT NULL DEFAULT FALSE,
    error_type VARCHAR(32) NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    stage VARCHAR(32) GENERATED ALWAYS AS ('ods') STORED,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, manifest_version, table_identity, attempt),
    FOREIGN KEY (task_id, stage) REFERENCES modeling_stages(task_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_ods_table_runs_latest
    ON ods_table_runs(task_id, manifest_version, table_identity, attempt DESC);
