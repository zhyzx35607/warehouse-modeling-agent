-- ================================================================
-- Wren Modeling Agent — Migration v16
-- dimension_registry.db_name: which database a dimension table lives in.
--
-- dimension_registry UNIQUE(dim_table, version) treated dim_table as the
-- full identity, so two sources with the same physical table name (e.g.
-- dim_financial_company in two warehouse databases) collided on dedupe.
-- A dedicated db_name column splits "physical table name" (dim_table, used
-- bare for SQL generation) from "database scope" (db_name, used for
-- dedupe + filtering). The uniqueness key becomes (dim_table, db_name,
-- version); legacy bare-name rows keep db_name = '' and stay compatible.
-- ================================================================

ALTER TABLE dimension_registry
    ADD COLUMN IF NOT EXISTS db_name TEXT NOT NULL DEFAULT '';

ALTER TABLE dimension_registry
    DROP CONSTRAINT IF EXISTS dimension_registry_dim_table_version_key;

ALTER TABLE dimension_registry
    ADD CONSTRAINT dimension_registry_dim_table_db_name_version_key
        UNIQUE (dim_table, db_name, version);

CREATE INDEX IF NOT EXISTS idx_dim_db_name
    ON dimension_registry(db_name);
