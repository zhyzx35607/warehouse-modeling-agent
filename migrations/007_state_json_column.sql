-- 007: Add state_json column to modeling_tasks for full state persistence.
-- Complements LangGraph checkpointer by storing a serialized snapshot of AgentState
-- in the task metadata table for direct API access without checkpoint deserialization.

ALTER TABLE modeling_tasks
    ADD COLUMN IF NOT EXISTS state_json JSONB NOT NULL DEFAULT '{}';

COMMENT ON COLUMN modeling_tasks.state_json IS
    'Serialized AgentState snapshot — complements LangGraph PostgreSQL checkpointer for fast API reads';
