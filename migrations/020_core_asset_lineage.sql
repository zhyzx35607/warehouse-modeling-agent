-- 020: Version lineage from core modeling artifacts to warehouse assets.

CREATE TABLE IF NOT EXISTS core_asset_lineage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id TEXT NOT NULL,
    core_model_version INTEGER NOT NULL CHECK (core_model_version > 0),
    warehouse_source_id UUID NOT NULL REFERENCES source_db_registry(id),
    asset_kind VARCHAR(24) NOT NULL
        CHECK (asset_kind IN ('dimension', 'metric', 'table', 'dag')),
    layer VARCHAR(8) NOT NULL CHECK (layer IN ('dim', 'dwd')),
    database_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    registry_id UUID,
    workflow_code BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_core_asset_lineage_source
    ON core_asset_lineage(warehouse_source_id, layer, database_name);
CREATE INDEX IF NOT EXISTS idx_core_asset_lineage_version
    ON core_asset_lineage(task_id, core_model_version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_core_asset_lineage_asset
    ON core_asset_lineage(task_id, core_model_version, asset_kind, database_name,
                          table_name, COALESCE(registry_id::text, ''),
                          COALESCE(workflow_code, 0));
