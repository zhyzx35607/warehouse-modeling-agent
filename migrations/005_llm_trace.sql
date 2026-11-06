-- LLM conversation trace — full prompt/response per LLM call, for skill
-- optimization and eval corpus shop (see eval/ graders).
-- Written fire-and-forget by LLMClient; queried via GET /tasks/{id}/llm-traces.

CREATE TABLE IF NOT EXISTS llm_trace (
    id            BIGSERIAL PRIMARY KEY,
    task_id       VARCHAR(36) NOT NULL,
    node          VARCHAR(64) NOT NULL DEFAULT '',
    skills        JSONB,                 -- skill names injected into the system prompt
    system_prompt TEXT,
    user_message  TEXT,
    response      TEXT,
    duration_ms   DOUBLE PRECISION DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_trace_task
    ON llm_trace (task_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llm_trace_node
    ON llm_trace (node);
