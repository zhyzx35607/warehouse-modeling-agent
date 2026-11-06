-- 013: Vector embeddings for field_semantics (semantic retrieval fallback)
-- Enables "same meaning, different name" lookup (amt vs amount vs 金额) so the
-- dictionary can serve fields whose exact table.field was never registered.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE field_semantics
    ADD COLUMN IF NOT EXISTS embedding vector(1024);  -- DashScope text-embedding-v3

-- HNSW supports incremental inserts (unlike IVFFlat) — safe on a growing table.
CREATE INDEX IF NOT EXISTS idx_field_semantics_embedding
    ON field_semantics USING hnsw (embedding vector_cosine_ops);
