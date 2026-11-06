-- 006: Per-datasource connection parameters (SSL toggle, extensible key-value).
-- MySQL connections default to SSL disabled; use_ssl=true opts in explicitly.

ALTER TABLE source_db_registry
    ADD COLUMN IF NOT EXISTS connection_params JSONB NOT NULL DEFAULT '{}';

COMMENT ON COLUMN source_db_registry.connection_params IS
    'Driver-level connection parameters, e.g. {"use_ssl": false, "charset": "utf8mb4", "connect_timeout": 15}';
