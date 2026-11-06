---
name: blueprint-match
description: Match a new ModelingTask against the Blueprint Library to find reusable design patterns. Compare source table signatures and domain tags, present top matches for human selection.
---

# Blueprint Matching

Search the Blueprint Library for reuse candidates before designing from scratch.

## Matching Algorithm

### 1. Table Signature Matching
For each blueprint in the library:
1. Extract source_tables_signature from the task's explored tables
2. Compare against blueprint's source_tables_signature using:
   - Table name similarity (Jaccard on table names)
   - Column overlap ratio per matched table
   - Key table presence (source_tables_signature filters)
3. Compute weighted score: 0.4 × table_name_sim + 0.4 × column_overlap + 0.2 × key_table_presence

### 2. Domain Tag Matching
- Compare blueprint tags against modeling_goal keywords
- Bonus score for domain tag matches

### 3. Present Top Matches
- Sort by score descending
- Present top 3 matches with:
  - Blueprint name, description, domain
  - Matched tables (with overlap ratio)
  - Unmatched tables (missing in source)
  - Adaptations needed (structural differences)

## Selection
- Human selects one blueprint (or "none" to design from scratch)
- If none match above threshold (score < 0.3), skip blueprint and design from scratch

## Adaptation
If a blueprint is selected:
- Generate a diff: blueprint structure vs actual source tables
- Map blueprint table patterns to actual source tables
- Produce adapted design as the starting point

## Learning
After task completes successfully:
- Abstract the verified design into a new blueprint
- Store in PG with version=1, source_task_id
- Increment usage_count on matched blueprint
