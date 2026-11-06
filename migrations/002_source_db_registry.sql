-- ================================================================
-- Wren Modeling Agent — Migration v2
-- Source Database Registry: persistent management of business DB connections
-- ================================================================

CREATE TABLE IF NOT EXISTS source_db_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,       -- Unique identifier (e.g. "ecommerce_mysql")
    description     TEXT NOT NULL DEFAULT '',   -- Human-readable description for agent prompts
    db_type         TEXT NOT NULL DEFAULT 'mysql',  -- mysql|postgresql|oracle|dm|kingbase|mongodb
    host            TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 3306,
    database_name   TEXT NOT NULL,              -- Database/schema name
    user_name       TEXT NOT NULL DEFAULT '',
    -- password is NEVER stored here — fetched from Secret Manager at runtime
    credential_ref  TEXT NOT NULL DEFAULT '',   -- Secret Manager key reference
    tags            TEXT[] NOT NULL DEFAULT '{}',     -- Business tags (e.g. 财务, 资产)
    business_desc   TEXT NOT NULL DEFAULT '',   -- Business domain description for LLM context
    table_count     INTEGER,                    -- Cached from last exploration
    last_explored_at TIMESTAMPTZ,               -- When last explored
    status          TEXT NOT NULL DEFAULT 'active',  -- active|inactive|error
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sdb_name    ON source_db_registry(name);
CREATE INDEX IF NOT EXISTS idx_sdb_type    ON source_db_registry(db_type);
CREATE INDEX IF NOT EXISTS idx_sdb_status  ON source_db_registry(status);
CREATE INDEX IF NOT EXISTS idx_sdb_tags    ON source_db_registry USING GIN (tags);
