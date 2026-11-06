-- ================================================================
-- Wren Modeling Agent — Migration v11
-- Field semantic dictionary (A-side): table.field -> Chinese meaning +
-- field role + enum values. Fed from the human DIM design workbook
-- (scripts/ingest_field_semantics.py) and reused by grill (skip
-- clarifications for known fields) and design context (inject semantics
-- into the LLM prompt). The B-side mirrors these rows into Neo4j as
-- SourceField nodes with HAS_FIELD edges during KG export.
-- See docs/wayfinder/map.md 字段语义字典.
-- ================================================================

CREATE TABLE IF NOT EXISTS field_semantics (
    id              SERIAL PRIMARY KEY,
    table_name      TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    chinese_meaning TEXT NOT NULL DEFAULT '',
    field_role      TEXT NOT NULL DEFAULT '',      -- 维度属性|维度外键|日期角色|审计质量|度量指标...
    enum_values     JSONB NOT NULL DEFAULT '[]',   -- ["0:未知","1:生效",...]
    source          TEXT NOT NULL DEFAULT 'manual', -- xlsx | manual | sampled
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (table_name, field_name)
);

CREATE INDEX IF NOT EXISTS idx_field_semantics_table ON field_semantics(table_name);

COMMENT ON TABLE field_semantics IS
    'Authoritative field semantics (table.field -> Chinese meaning + role). '
    'Grill skips clarification for covered fields; design prompts inject them; '
    'KG export mirrors them as SourceField nodes.';
