-- ================================================================
-- Wren Modeling Agent — Migration v10
-- Persistent pause registry: tasks paused at wait_human / human_approval
-- register here so ANY engine instance (or a restarted one) can resume them.
-- resume_* writes the decision here; the paused node polls this row while
-- waiting (in-process asyncio.Event remains a fast path, PG is the source
-- of truth). See wayfinder analysis of task 85b063e9 / 40138835.
-- ================================================================

CREATE TABLE IF NOT EXISTS task_pauses (
    task_id      TEXT PRIMARY KEY,
    pause_type   TEXT NOT NULL,                     -- clarification | approval
    status       TEXT NOT NULL DEFAULT 'paused',    -- paused | resumed
    payload      JSONB NOT NULL DEFAULT '{}',       -- {cid, answer} | {aid, approved, comment}
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resumed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_task_pauses_status ON task_pauses(status);

COMMENT ON TABLE task_pauses IS
    'Cross-engine pause registry: paused tasks are resumed via PG so the '
    'decision survives engine restarts and works from any API instance.';
