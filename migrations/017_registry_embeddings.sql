-- ================================================================
-- Wren Modeling Agent — Migration v17
-- Vector embeddings for metric_registry / dimension_registry entries.
--
-- Enables modeling-goal semantic relevance filtering: the design layer
-- embeds the modeling goal once and retrieves the semantically related
-- registry entries (metric_name+grain+definition / dim_name+dim_table+grain)
-- via HNSW cosine search — scoped by the task's layer databases first
-- (DWS → dim/dwd, ADS → dws), then by goal similarity.
-- ================================================================

ALTER TABLE metric_registry
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_metric_registry_embedding
    ON metric_registry USING hnsw (embedding vector_cosine_ops);

ALTER TABLE dimension_registry
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_dim_registry_embedding
    ON dimension_registry USING hnsw (embedding vector_cosine_ops);
