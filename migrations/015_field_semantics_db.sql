-- ================================================================
-- Wren Modeling Agent — Migration v15
-- field_semantics.db_name: which database a table belongs to.
--
-- The semantic dictionary previously stored only bare table names, so the
-- KG mirror (SourceField nodes) carried no database scope and the runtime
-- fallback could not filter by the current task's relevant databases. The
-- backfill script always knew the layer database while harvesting; it just
-- never persisted it. This column enables db-scoped lookups going forward;
-- legacy rows default to '' (bare/unscoped) and remain visible everywhere.
-- ================================================================

ALTER TABLE field_semantics
    ADD COLUMN IF NOT EXISTS db_name TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_field_semantics_db
    ON field_semantics(db_name);
