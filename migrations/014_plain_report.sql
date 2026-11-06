-- Migration 014: Persist a business-friendly companion report alongside the
-- existing technical construction report.

ALTER TABLE task_artifacts
    ADD COLUMN IF NOT EXISTS plain_report TEXT;
