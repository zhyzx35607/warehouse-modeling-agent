-- ================================================================
-- Wren Modeling Agent — Migration v12
-- Per-approval-round narrative blocks for the design_plan approval
-- detail page (wayfinder effort: design_plan 审批信息细化, T-01/T-02).
--
-- Three LLM narrative blocks are generated in the background when the
-- task pauses at human_approval (goal_interpretation /
-- metric_interpretation / expected_impact), plus a computed 'risk'
-- block carrying validation failures + known-risk markers. Each
-- approval round creates a fresh approval item id, so rows are keyed
-- by (approval_id, block) — revision loops version naturally.
-- status: pending | ready | failed (LLM blocks); risk is always ready.
-- ================================================================

CREATE TABLE IF NOT EXISTS approval_narratives (
    approval_id  UUID NOT NULL REFERENCES task_approvals(id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL,
    block        TEXT NOT NULL,      -- goal_interpretation | metric_interpretation | expected_impact | risk
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | ready | failed
    content_json JSONB,
    error        TEXT,
    attempts     INT  NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (approval_id, block)
);

CREATE INDEX IF NOT EXISTS idx_approv_narr_approval ON approval_narratives(approval_id);
CREATE INDEX IF NOT EXISTS idx_approv_narr_task     ON approval_narratives(task_id);

COMMENT ON TABLE approval_narratives IS
    'Approval-round narrative blocks: LLM-generated goal/metric/impact '
    'interpretations plus the computed risk block, keyed per approval '
    'round for versioned history review.';
