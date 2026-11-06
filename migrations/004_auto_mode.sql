-- ================================================================
-- Migration 004: Auto (unattended) task mode + clarification recommendations
--
-- 1. modeling_tasks.mode — task-level HITL mode: 'manual' pauses at human
--    nodes; 'auto' runs unattended (agent adopts LLM recommendations and
--    auto-approves).
-- 2. task_clarifications.recommended / recommended_reason — the LLM's
--    recommended answer for each clarification, used by auto mode and
--    highlighted in the dashboard in manual mode.
-- ================================================================

ALTER TABLE modeling_tasks
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'manual';  -- manual | auto

ALTER TABLE task_clarifications
    ADD COLUMN IF NOT EXISTS recommended TEXT,                     -- LLM recommended answer (NULL = rule-based, no recommendation)
    ADD COLUMN IF NOT EXISTS recommended_reason TEXT NOT NULL DEFAULT '';

-- ================================================================
-- Post-migration: log success
-- ================================================================
DO $$
BEGIN
    RAISE NOTICE 'Wren Agent migration v4 completed: modeling_tasks.mode, task_clarifications.recommended/recommended_reason';
END $$;
