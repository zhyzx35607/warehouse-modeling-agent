---
name: schema-drift
description: Detect schema changes in source databases before each DAG execution. Compare current schema against stored snapshots, classify changes as breaking or additive, generate auto-fix proposals.
---

# Schema Drift Detection

Run before every DAG execution. Detect and respond to source table schema changes.

## 1. Snapshot Comparison
For each source table in the task:
1. Read the last `SchemaSnapshot` from PostgreSQL
2. Capture a new `SchemaSnapshot` from the current source schema
3. Compare: added columns, removed columns, type changes, renamed tables

## 2. Classification

| Change | DriftType | Breaking? | Action |
|--------|-----------|-----------|--------|
| Column removed | COLUMN_REMOVED | YES | Pause + auto-fix |
| Column type changed | COLUMN_TYPE_CHANGED | YES | Pause + auto-fix |
| Table removed | TABLE_REMOVED | YES | Pause + escalation |
| Column renamed | COLUMN_RENAMED | YES | Pause + auto-fix (detect semantic match) |
| Column added | COLUMN_ADDED | NO | Alert, do not block |
| Table renamed | TABLE_RENAMED | YES | Pause + escalation |

## 3. Auto-Fix Generation (Breaking Changes)

### Column Removed
1. Find all downstream SQL that references the removed column
2. Generate ALTER TABLE statements to remove the column from DW layers
3. Generate updated DAG SQL without the column reference

### Column Type Changed
1. Check if the new type is compatible with the current Doris type
2. If compatible (e.g., INT → BIGINT): generate ALTER TABLE MODIFY COLUMN
3. If incompatible (e.g., VARCHAR(50) → VARCHAR(200)): check downstream constraints

### Column Renamed
1. Compare data samples between old column and other columns
2. Detect semantic matches (same sample values, same position)
3. Generate rename migration SQL

## 4. Human Approval
All auto-fix SQL proposals must be approved by a human before application.
Present:
- The drift detected (what changed)
- The impact (which DW layers are affected)
- The proposed fix (SQL diff)
- Option to approve, reject, or provide custom fix

## 5. Additive Changes (Non-Breaking)
For new columns: alert human with column details and ask:
- Should this column be added to DW layers? (yes/no/later)
- If yes, update design plan and generate migration SQL
