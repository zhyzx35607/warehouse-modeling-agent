-- ================================================================
-- Wren Modeling Agent — Migration v9
-- DAG table mapping: deployed table -> DolphinScheduler workflow code.
-- Written by dag_deploy on success (all modes); queried by warehouse-mode
-- deployment to resolve upstream DWD/DWM tables to their already-deployed
-- workflows. Historical DAGs without a mapping row fall back to a
-- task-name search in DolphinScheduler (see dag-deploy skill).
-- ================================================================

CREATE TABLE IF NOT EXISTS dag_table_map (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name    TEXT NOT NULL,              -- fully qualified as deployed (db.table)
    project_code  BIGINT NOT NULL,            -- DolphinScheduler project code
    workflow_code BIGINT NOT NULL,            -- DolphinScheduler workflow code
    task_id       TEXT NOT NULL,              -- wren task that deployed the DAG
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A table maps to exactly one workflow (the DW workflow contains many tables).
CREATE UNIQUE INDEX IF NOT EXISTS idx_dag_table_map_table ON dag_table_map(table_name);
CREATE INDEX IF NOT EXISTS idx_dag_table_map_workflow ON dag_table_map(workflow_code);

COMMENT ON TABLE dag_table_map IS
    'Table -> DolphinScheduler workflow mapping, written at deploy time. Used by warehouse mode to wire DEPENDENT tasks onto upstream DWD/DWM DAGs.';
