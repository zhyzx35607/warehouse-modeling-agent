---
name: explore-schema
description: Two-phase database schema exploration for the Wren Modeling Agent. Phase 1 (light, auto): table list, column types, row estimates, sample 1000 rows. Phase 2 (deep, human-confirmed): COUNT DISTINCT, JOIN cardinality verification, NULL ratios.
---

# Schema Exploration

Proceed in two mandatory phases. Never skip a phase or combine them.

## Phase 1: Light Exploration (automatic)

For each source database:
1. Query `information_schema.tables` for table list and approximate row counts
2. Query `information_schema.columns` for column names, types, nullability, defaults
3. For each table, run `SELECT * FROM {table} LIMIT 1000` for sample values
4. Identify primary keys and indexes from `information_schema`
5. Map each source column type to a Doris type using the TYPE_MAPPING table
6. Flag any types that have no Doris equivalent as INCOMPATIBLE

## Phase 2: Deep Exploration (requires human approval)

Generate a list of items and present for human approval before executing:
1. COUNT DISTINCT on high-cardinality columns (estimate query cost first)
2. JOIN verification for implicit table relationships (check actual cardinality)
3. Full NULL ratio analysis on nullable columns
4. Value distribution histograms for key dimension columns
5. Date range analysis for time-partitioned tables

## Output

Produce a `SchemaExploration` with:
- All table schemas (Phase 1)
- Verified relationships with actual cardinality (Phase 2)
- Type mapping results
- Flagged incompatibilities
- Sensitive field candidates (delegated to sensitive-field detection step)

## Warehouse Mode (source_mode="warehouse")

When exploring a **Doris warehouse source** (db_type=doris with `connection_params.layers`):

- Exploration runs **per layer database** — dim/dwd/dwm/dws are explorable, ods/ads are not. One database may host several layers (wren_dw-style); classify by table-name prefix and skip `ods_*`.
- Exploration keys are `"<source_id>#<layer>"` (e.g. `wh-cluster#dwd`).
- Doris has **no FK metadata** — `get_foreign_keys()` returns empty. Discover relationships from: table-name conventions (dim_/dwd_/fct_ groups + domain prefixes), field-name matching (`<dim>_id` / `dim_sk` surrogate keys), and `metric_registry.definition_sql` references when populated.
- Row estimates come from `SHOW TABLE STATUS` (Doris does not populate `information_schema.TABLE_ROWS`).
- Candidate relationships are verified by sampling join cardinality; high-confidence ones are accepted automatically, low-confidence ones go to the deep-explore confirmation list.
