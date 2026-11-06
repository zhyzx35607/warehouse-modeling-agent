-- 008: Add unique constraints on task_clarifications.id and task_approvals.id
-- Prevents duplicate INSERTs when nodes re-execute during crash recovery.
-- Existing ON CONFLICT (id) DO NOTHING in approval.py:113 requires this constraint.
-- NOTE: PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS — guard via pg_constraint.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_task_clarifications_id'
          AND conrelid = 'task_clarifications'::regclass
    ) THEN
        ALTER TABLE task_clarifications
            ADD CONSTRAINT uq_task_clarifications_id UNIQUE (id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_task_approvals_id'
          AND conrelid = 'task_approvals'::regclass
    ) THEN
        ALTER TABLE task_approvals
            ADD CONSTRAINT uq_task_approvals_id UNIQUE (id);
    END IF;
END $$;
