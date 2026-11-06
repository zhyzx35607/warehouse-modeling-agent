---
name: dag-deploy
description: Deploy the DW design to Dolphin Scheduler. Create project, data sources, DAGs with DataX and/or Flink CDC components, execute manually, and loop-retry on transient failures.
---

# DAG Deployment to Dolphin Scheduler

Deploy the complete data pipeline to Dolphin Scheduler.

## 1. Project Setup
- Create a DS project: `{task_id}_{modeling_goal_short}`
- Set description from modeling goal

## 2. Data Source Registration

### Target (Doris)
- Pre-created by humans — search by name, do not create
- Verify connectivity before proceeding

### Source Databases
- Preferred: Call MCP's `register_as_ds_source()` (MCP uses its own credentials)
- Fallback: Get credentials from Secret Manager, call DS API to create data source
- NEVER expose credentials in agent state or logs

## 3. DAG Creation

### DataX DAGs
For each ODS table using DataX:
1. Create a DataX task component with the generated JSON config
2. Set schedule: based on incremental strategy (daily/weekly)
3. Add upstream dependency: none (ODS can all run in parallel)

### Flink CDC DAGs
For each ODS table using Flink CDC:
1. Create a Flink task component with CDC config
2. Set as long-running task (no schedule — runs continuously)
3. Separate DAG from DataX tasks

### DW Layer DAGs
For DIM → DWD → DWS → ADS:
1. Create SQL task components for each layer
2. Set dependencies: DIM can be parallel, DWD depends on ODS+DIM, DWS depends on DWD, ADS depends on DWS+DIM
3. Use Doris internal SQL execution (not DataX) for DW layer transformations

## 4. Manual Execution
1. Trigger ODS `_sync` DAGs concurrently (cap: `AGENT_EXECUTE_CONCURRENCY`, default 8). Do not wait for one table's poll+logs before starting the next.
2. After every ODS run has finished, trigger the DW transform DAG
3. Monitor task instance status and collect logs from each task instance
4. On failure, classify and repair, then **redeploy and re-run only the unsuccessful DAGs**. Do not recreate workflows that already succeeded.

## 5. Loop-Retry Rules
- **Transient errors**: Exponential backoff (30s, 60s, 120s), retry up to 3 times
- **Resource errors**: Alert human, wait for resolution signal, then retry
- **Data/Logic/Config/Dependency errors**: Do NOT retry — escalate to human immediately

## 6. Namespace Convention
- DS project: `{task_id}__{domain}` (e.g., `abc123__trading`)
- DAG names: `{task_id}__{layer}__{table}` (only task_id prefix for isolation)

## Warehouse Mode (source_mode="warehouse")

- The DW workflow starts with **DEPENDENT tasks** — one per upstream fact table — waiting for the already-deployed upstream DWD/DWM workflow instances (day cycle). Resolution order: `dag_table_map` (PG, migration 009) → DS task-name search fallback (`sql_*_<table>` task names). Unresolved upstreams are warned about and skipped (non-fatal).
- DWS/ADS SQL tasks run against **per-layer DS datasources** (`doris_dws` / `doris_ads`, pointing at `layers["dws"]` / `layers["ads"]`) instead of the shared `doris_target`.
- The workflow schedule **inherits the first upstream workflow's cron** (queried from DS); fallback remains the monthly default.
- Every deployed table is recorded into `dag_table_map` (all modes) so future warehouse tasks can wire DEPENDENT tasks without searching.
