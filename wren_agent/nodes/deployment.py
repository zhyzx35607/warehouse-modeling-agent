"""Deployment & execution nodes — DS DAG deploy, execute, error classify, retry."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from wren_agent.models.task import ErrorClassification, ErrorType, ExecutionResult, TaskPhase
from wren_agent.state import AgentState

logger = logging.getLogger(__name__)

# Max consecutive dag_deploy failures before an auto-mode task fails outright.
# Without this, auto mode ping-pongs deploy → error_handling → auto-approve →
# deploy until the graph recursion limit (500) kills the task.
MAX_DEPLOY_FAILURES = 2

# Metadata persistence must never leave the graph indefinitely parked on the
# drift node when PostgreSQL is slow or temporarily unavailable.
SCHEMA_SNAPSHOT_TIMEOUT_SECONDS = 10.0
SCHEMA_SNAPSHOT_MAX_CONCURRENCY = 8


async def _mark_deploy_failure(state: AgentState, error_msg: str) -> None:
    """Record a dag_deploy failure and trip the deploy circuit breaker.

    The breaker fires for BOTH modes (previously auto-only): a manual-mode
    deploy failure used to route error_handling → human_approval → re-deploy
    → fail → … forever, re-surfacing the approval each round (observed on
    task 40138835). Two consecutive failures always fail the task.
    """
    state["deploy_succeeded"] = False
    count = state.get("deploy_failure_count", 0) + 1
    state["deploy_failure_count"] = count
    logger.warning(
        "[%s] dag_deploy failure #%d: %s",
        state.get("task_id", "")[:8], count, error_msg,
    )

    if count >= MAX_DEPLOY_FAILURES:
        from wren_agent.models.task import TaskStatus
        from wren_agent.tools.event_emitter import emit_error, emit_status

        msg = (
            f"dag_deploy failed {count} consecutive time(s) — "
            f"failing the task instead of looping. Last error: {error_msg}"
        )
        state["errors"] = state.get("errors", []) + [msg]
        state["status"] = TaskStatus.FAILED
        emit_error(state, msg)
        emit_status(state, TaskStatus.FAILED.value)
        try:
            from wren_agent.engine import update_task_pg_status
            await update_task_pg_status(
                state.get("task_id", ""), TaskStatus.FAILED.value, "dag_deploy"
            )
        except Exception:
            logger.warning("Failed to persist FAILED status to PG", exc_info=True)


def _synthetic_failure(instance_id: str, message: str) -> ExecutionResult:
    """Build a failed ExecutionResult for early-exit paths in execute_tasks."""
    return ExecutionResult(
        task_instance_id=instance_id,
        success=False,
        error=ErrorClassification(
            task_instance_id=instance_id,
            error_type=ErrorType.CONFIG,
            raw_log=message,
            summary=message,
            action="escalate",
        ),
    )


async def detect_schema_drifts(state: AgentState) -> AgentState:
    """Detect schema drifts before ODS design.

    Compares current source table schemas against stored snapshots
    from the previous modeling run. Classifies changes as breaking or additive.

    Warehouse mode: skipped entirely (T-07) — warehouse tables are managed by
    this system, so structure changes flow through its own change process.
    """
    from wren_agent.models.task import DriftType, SchemaDriftResult
    from wren_agent.tools.db_explorer import take_schema_snapshot
    from wren_agent.tools.pg_store import (
        load_latest_schema_snapshots,
        store_schema_snapshot,
    )

    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    if state.get("source_mode") == "warehouse":
        state["schema_drifts"] = []
        return state

    current_snapshots = {}
    prior_snapshots = {}
    drifts: list[SchemaDriftResult] = []
    explorations = state.get("explorations", {})
    persisted_prior = state.get("prior_snapshots", {})
    store_jobs: list[tuple[str, object]] = []

    for db_id, exploration in explorations.items():
        try:
            baseline = await asyncio.wait_for(
                load_latest_schema_snapshots(state["task_id"], db_id),
                timeout=SCHEMA_SNAPSHOT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Unable to load schema baseline for %s: %s",
                state.get("task_id", "")[:8], db_id, exc,
            )
            state["warnings"] = state.get("warnings", []) + [
                f"Schema snapshot baseline unavailable for '{db_id}': {exc}"
            ]
            baseline = {}

        # Preserve compatibility with recovered checkpoints that already carry
        # snapshots even when the metadata store is temporarily unavailable.
        for table in exploration.tables:
            checkpoint_prior = (
                persisted_prior.get(f"{db_id}:{table.name}")
                or persisted_prior.get(table.name)
            )
            if table.name not in baseline and checkpoint_prior is not None:
                baseline[table.name] = checkpoint_prior

        for table_name, prior in baseline.items():
            prior_snapshots[f"{db_id}:{table_name}"] = prior

        current_tables = {table.name: table for table in exploration.tables}
        catalog = set(getattr(exploration, "catalog_table_names", None) or [])

        # Tables present in the previous baseline but absent now are breaking
        # drifts — but only when they are also gone from the source catalog.
        # An explore cap (or keyword filter) leaving a live table undescribed
        # is not a deletion (tasks a8cc2a03 / 9fcfcdbb).
        for table_name in set(baseline) - set(current_tables):
            if catalog and table_name in catalog:
                continue
            prior = baseline[table_name]
            prior_fields = getattr(
                prior, "fields",
                prior.get("fields", {}) if isinstance(prior, dict) else {},
            )
            drifts.append(SchemaDriftResult(
                table_name=table_name,
                drift_type=DriftType.TABLE_REMOVED,
                details=f"Table '{table_name}' removed from source '{db_id}'",
                is_breaking=True,
                previous_snapshot=prior_fields,
                current_snapshot={},
            ))

        for table in exploration.tables:
            snap = take_schema_snapshot(table, db_id)
            current_snapshots[f"{db_id}:{table.name}"] = snap
            store_jobs.append((
                f"{db_id}:{table.name}",
                store_schema_snapshot(
                    task_id=state["task_id"],
                    table_name=table.name,
                    source_db_id=db_id,
                    fields=snap.fields,
                    primary_keys=snap.primary_keys,
                ),
            ))

            prior = baseline.get(table.name)
            if prior is None:
                continue

            # Collect current fields
            curr_fields = {f.name: f.data_type for f in table.fields}
            prior_fields = getattr(prior, "fields", prior.get("fields", {}) if isinstance(prior, dict) else {})

            curr_set = set(curr_fields.keys())
            prior_set = set(prior_fields.keys())

            # Removed columns (breaking)
            for col in prior_set - curr_set:
                drifts.append(SchemaDriftResult(
                    table_name=table.name,
                    drift_type=DriftType.COLUMN_REMOVED,
                    details=f"Column '{col}' removed (was type {prior_fields[col]})",
                    is_breaking=True,
                    previous_snapshot=prior_fields,
                    current_snapshot=curr_fields,
                    auto_fix_sql=_generate_column_removal_fix(table.name, col),
                ))

            # Added columns (additive, non-breaking)
            for col in curr_set - prior_set:
                drifts.append(SchemaDriftResult(
                    table_name=table.name,
                    drift_type=DriftType.COLUMN_ADDED,
                    details=f"New column '{col}' added (type {curr_fields[col]})",
                    is_breaking=False,
                    previous_snapshot=prior_fields,
                    current_snapshot=curr_fields,
                ))

            # Type changes (breaking)
            for col in prior_set & curr_set:
                if prior_fields[col] != curr_fields[col]:
                    drifts.append(SchemaDriftResult(
                        table_name=table.name,
                        drift_type=DriftType.COLUMN_TYPE_CHANGED,
                        details=f"Column '{col}' type changed: {prior_fields[col]} → {curr_fields[col]}",
                        is_breaking=True,
                        previous_snapshot=prior_fields,
                        current_snapshot=curr_fields,
                        auto_fix_sql=_generate_type_change_fix(table.name, col, prior_fields[col], curr_fields[col]),
                    ))

    if store_jobs:
        semaphore = asyncio.Semaphore(SCHEMA_SNAPSHOT_MAX_CONCURRENCY)

        async def _store_with_limit(job):
            async with semaphore:
                return await asyncio.wait_for(
                    job, timeout=SCHEMA_SNAPSHOT_TIMEOUT_SECONDS,
                )

        results = await asyncio.gather(*(
            _store_with_limit(job) for _, job in store_jobs
        ), return_exceptions=True)
        for (snapshot_key, _), result in zip(store_jobs, results):
            if isinstance(result, Exception):
                logger.warning(
                    "[%s] Unable to store schema snapshot %s: %s",
                    state.get("task_id", "")[:8], snapshot_key, result,
                )
                state["warnings"] = state.get("warnings", []) + [
                    f"Schema snapshot persistence failed for '{snapshot_key}': {result}"
                ]

    state["schema_drifts"] = drifts
    state["prior_snapshots"] = prior_snapshots
    state["current_snapshots"] = current_snapshots

    if drifts:
        breaking = [d for d in drifts if d.is_breaking]
        additive = [d for d in drifts if not d.is_breaking]
        state["warnings"] = state.get("warnings", []) + [
            f"Schema drift detected: {len(breaking)} breaking, {len(additive)} additive changes"
        ]

    return state


async def generate_drift_fixes(state: AgentState) -> AgentState:
    """Generate auto-fix SQL proposals for breaking schema drifts.

    For each breaking drift:
    - Column removed → ALTER TABLE DROP COLUMN in DW layers
    - Column type changed → ALTER TABLE MODIFY COLUMN if compatible
    - Column renamed → detect semantic match, generate rename migration

    All fixes require human approval before application.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    drifts = state.get("schema_drifts", [])
    plan = state.get("design_plan")

    if any(drift.is_breaking for drift in drifts):
        state["approval_context"] = "schema_drift"

    for drift in drifts:
        if not drift.is_breaking or not drift.auto_fix_sql:
            continue

        # In production: refine auto-fix SQL by checking all downstream
        # DW layers that reference this column
        if plan and plan.sql_artifacts:
            affected_sql = [
                a for a in plan.sql_artifacts
                if a.target_table and drift.table_name.lower() in a.target_table.lower()
            ]
            if affected_sql:
                drift.details += f"\nAffects DW tables: {', '.join(a.target_table for a in affected_sql)}"

    # Sandbox validation for drift fix SQL
    await _sandbox_validate_drift_fixes(state, drifts)

    return state


def _quartz_cron(frequency: str) -> str:
    """Map a schedule frequency to a DS Quartz cron expression.

    DS uses 7-field Quartz crons (sec min hour dom month dow year),
    NOT unix 5-field crons. hourly/monthly added for warehouse mode
    schedule inheritance (wayfinder T-11) — previously they degraded to daily.
    """
    if frequency == "weekly":
        return "0 0 0 ? * 1 *"      # Sunday 00:00
    if frequency == "monthly":
        return "0 0 0 1 * ? *"      # 1st of month 00:00
    if frequency == "hourly":
        return "0 0 * * * ? *"      # top of every hour
    return "0 0 0 * * ? *"          # daily 00:00


def _flink_cdc_shell_script(dag_name: str, flink_config: str) -> str:
    """Wrap a Flink CDC config into a SHELL task script.

    Writes the config to a file and hands it to a site-configurable submit
    command (FLINK_CDC_SUBMIT env on the DS worker). Until that env is
    configured the task logs the config path and succeeds as a no-op, which
    keeps the deploy verifiable end-to-end without a Flink cluster.
    """
    return (
        f"config_path=/tmp/{dag_name}.json\n"
        f"cat > \"$config_path\" <<'WREN_EOF'\n"
        f"{flink_config}\n"
        f"WREN_EOF\n"
        f"${{FLINK_CDC_SUBMIT:-echo '[wren] FLINK_CDC_SUBMIT not configured; config written to'}} \"$config_path\"\n"
    )


# ── Warehouse-mode deployment helpers (wayfinder T-11) ──────────


def _warehouse_datasource_name(layer: str, database: str) -> str:
    """DS datasource name for a warehouse layer's landing database.

    Per-(layer, database) naming isolates landing choices — the old shared
    `doris_dws`/`doris_ads` names were reused across tasks with different
    landings, silently creating tables in the first task's database
    (task 67b6907f).
    """
    return f"doris_{layer.lower()}_{database}"


def _warehouse_layers(state: AgentState) -> dict[str, str]:
    """Return the first warehouse source's EFFECTIVE single-db-per-layer mapping.

    Applies the task-level landing override (`connection_params.landing`,
    chosen in the dashboard 库级落库选择): a layer's chosen database wins;
    otherwise the first database of a multi-db layer is used. A single str
    per layer is guaranteed so callers never receive a list (e.g. DS
    datasource creation needs one database).
    """
    for sdb in state.get("source_databases", []):
        if hasattr(sdb, "model_dump"):
            sdb = sdb.model_dump()
        cp = sdb.get("connection_params") or {}
        layers = cp.get("layers") or {}
        if not (isinstance(layers, dict) and layers):
            continue
        landing = cp.get("landing") or {}
        out: dict[str, str] = {}
        for layer, val in layers.items():
            dbs = val if isinstance(val, list) else ([val] if val else [])
            dbs = [str(d).strip() for d in dbs if str(d).strip()]
            chosen = str(landing.get(layer) or "").strip()
            out[layer] = chosen if chosen in dbs else (dbs[0] if dbs else "")
        return out
    return {}


def task_relevant_db_names(state: AgentState) -> set[str]:
    """Databases the current task actually touches (registry/KG filter scope).

    Warehouse mode: landing/configured layer databases (`_warehouse_layers`)
    ∪ the schema names of every explored table. Business mode has no layer
    databases — returns empty, and callers treat empty as "no filtering"
    (inject everything, keeping pre-filter behavior).
    """
    if state.get("source_mode") != "warehouse":
        return set()
    dbs: set[str] = set()
    try:
        for db in _warehouse_layers(state).values():
            if db:
                dbs.add(str(db).strip())
        for exp in (state.get("explorations") or {}).values():
            for t in getattr(exp, "tables", []) or []:
                schema = str(getattr(t, "schema_name", None) or "").strip()
                if schema:
                    dbs.add(schema)
    except Exception:
        pass
    return dbs


def layer_relevant_db_names(state: AgentState, layers: set[str]) -> set[str]:
    """Databases of the given layers — for layer-scoped registry filtering.

    DWS design consumes dim/dwd-layer sources, so its registry context is
    scoped to `layer_relevant_db_names(state, {"dim", "dwd"})`; ADS anchors on
    dws-layer sources → `{"dws"}`. Combines the FULL configured layer database
    lists (`connection_params.layers`, NOT just the single landing db) with
    the schema names of explored tables whose exploration key ends in one of
    the requested layers. Business mode returns empty (callers treat empty as
    "no filtering").

    Registry entries live in the SOURCE layer databases (e.g.
    `dws_ecommerce_orders_sh.dws_...`) — filtering by the landing db alone
    (`db_dws`) matched nothing and silently skipped the whole registry
    context (task d28431de: 98 receivable-domain metrics existed but zero
    reached the design prompts). The landing single-db mapping
    (`_warehouse_layers`) is for DS deployment, not for registry scope.
    """
    if state.get("source_mode") != "warehouse":
        return set()
    requested = {str(l).lower() for l in layers}
    dbs: set[str] = set()
    try:
        # Full layer database list from the source connection params — every
        # candidate prefix of that layer (landing db + real catalog schemas).
        for sdb in state.get("source_databases", []):
            if hasattr(sdb, "model_dump"):
                sdb = sdb.model_dump()
            cp = sdb.get("connection_params") or {}
            layers_cfg = cp.get("layers") or {}
            for layer, val in layers_cfg.items():
                if str(layer).lower() not in requested:
                    continue
                vals = val if isinstance(val, list) else ([val] if val else [])
                for db in vals:
                    db = str(db).strip()
                    if db:
                        dbs.add(db)
        for key, exp in (state.get("explorations") or {}).items():
            layer = str(key).rsplit("#", 1)[-1].lower() if "#" in str(key) else ""
            if layer not in requested:
                continue
            for t in getattr(exp, "tables", []) or []:
                schema = str(getattr(t, "schema_name", None) or "").strip()
                if schema:
                    dbs.add(schema)
    except Exception:
        pass
    return dbs


def registry_db_name(state: AgentState, table_name: str, layer: str) -> str:
    """The database a registry entry's table lives in for this task.

    Prefers the explored table's real schema name (the Doris catalog source
    the backfill script also uses) — a dim_table like `dim_financial_company`
    matches the explored table either by its full name or by its `dim_`-
    stripped source name. Falls back to the configured/landing layer database,
    then ''.
    """
    if state.get("source_mode", "business_db") == "business_db":
        return str((state.get("landing") or {}).get(layer) or "")
    try:
        want = (table_name or "").strip()
        bare = want[4:] if want.startswith("dim_") else want
        for exp in (state.get("explorations") or {}).values():
            for t in getattr(exp, "tables", []) or []:
                name = (getattr(t, "name", None) or "").strip()
                if name == want or name == bare:
                    schema = str(getattr(t, "schema_name", None) or "").strip()
                    if schema:
                        return schema
    except Exception:
        pass
    return _warehouse_layers(state).get(layer, "") or ""


async def _record_dag_map(
    table_name: str,
    project_code: int,
    workflow_code: int,
    task_id: str,
) -> None:
    """Persist table -> workflow mapping (migration 009); never fatal."""
    from wren_agent.tools.pg_store import record_dag_table_map
    try:
        await record_dag_table_map(table_name, int(project_code), int(workflow_code), task_id)
    except Exception:
        logger.warning(
            "Failed to record dag_table_map for '%s'", table_name, exc_info=True,
        )


async def _search_upstream_in_ds(ds, table_name: str) -> tuple[str, str] | None:
    """Fallback: find (project_code, workflow_code) whose task names reference
    the table. Task names embed the target table (`sql_dwd_*_<table>`), so a
    LIKE match across workflow definitions resolves historical DAGs that
    predate dag_table_map (wayfinder T-11).
    """
    try:
        projects = await ds.list_projects()
        for project in projects:
            pcode = str(project.get("code", ""))
            if not pcode:
                continue
            for wf in await ds.list_workflows(int(pcode)):
                try:
                    detail = await ds.get_workflow(int(pcode), int(wf.get("code", 0)))
                except Exception:
                    continue
                tasks = (detail.get("taskDefinitionList") or detail.get("tasks") or [])
                for task in tasks:
                    name = task.get("name") or task.get("taskName") or ""
                    if table_name in name:
                        return (pcode, str(wf.get("code", "")))
    except Exception:
        logger.warning("Upstream search in DS failed", exc_info=True)
    return None


async def _resolve_warehouse_upstreams(
    ds, state: AgentState, plan,
) -> list[tuple[str, str, str]]:
    """Resolve upstream fact tables to (table, project_code, workflow_code).

    Sources: DWSDesign.source_tables (+ legacy source_dwd_table). Resolution:
    dag_table_map first, then DS task-name search; unresolved tables are
    warned about and skipped (non-fatal — T-11).
    """
    from wren_agent.tools.pg_store import lookup_dag_table_map

    tables: list[str] = []
    for dws in (plan.dws_designs or []):
        srcs = list(getattr(dws, "source_tables", None) or [])
        if getattr(dws, "source_dwd_table", "") and dws.source_dwd_table not in srcs:
            srcs.append(dws.source_dwd_table)
        tables.extend(srcs)

    seen: set[str] = set()
    deduped = [t for t in tables if not (t in seen or seen.add(t))]

    refs: list[tuple[str, str, str]] = []
    for table in deduped:
        try:
            row = await lookup_dag_table_map(table)
            if row:
                pcode, wcode = str(row["project_code"]), str(row["workflow_code"])
                # The mapped workflow may have been deleted from DS (e.g. its
                # task was cancelled and the project cleaned up) — verify
                # before use, otherwise DS rejects the DEPENDENT node as
                # "parameter invalid" (observed on task 6521caff pointing at
                # deleted task a0a63385's project).
                try:
                    await ds.get_workflow(int(pcode), int(wcode))
                    refs.append((table, pcode, wcode))
                    continue
                except Exception:
                    state["warnings"] = state.get("warnings", []) + [
                        f"[DAG] dag_table_map entry for '{table}' is stale "
                        f"(workflow {wcode} no longer exists) — falling back to DS search"
                    ]
        except Exception:
            pass
        found = await _search_upstream_in_ds(ds, table)
        if found:
            refs.append((table, found[0], found[1]))
        else:
            state["warnings"] = state.get("warnings", []) + [
                f"[DAG] upstream '{table}' has no deployed workflow — DEPENDENT skipped"
            ]
    return refs


async def _resolve_upstream_schedule(ds, ref: tuple[str, str, str]) -> str | None:
    """Copy the first upstream workflow's schedule cron (T-11)."""
    try:
        schedules = await ds.list_schedules(int(ref[1]), int(ref[2]))
        for s in schedules:
            cron = (s.get("crontab") or s.get("cronExpression") or "").strip()
            if cron:
                return cron
    except Exception:
        logger.warning("Upstream schedule lookup failed", exc_info=True)
    return None


async def dag_deploy(state: AgentState) -> AgentState:
    """Deploy DAGs to Dolphin Scheduler via cli-anything core.

    Steps:
    1. Create DS project (idempotent: wren_{task_id[:8]})
    2. Register data sources for each source DB; locate the Doris target
    3. Create ODS workflows with DataX/Flink CDC tasks
    4. Create the DW layer workflow with SQL tasks (DIM → DWD → DWS → ADS)
    5. Release workflows and bring schedules online
    """
    if state.get("source_mode", "business_db") == "business_db":
        from wren_agent.tools.core_handoff import ensure_core_databases
        await ensure_core_databases(state)
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DAG_DEPLOY

    plan = state.get("design_plan")
    if plan is None:
        state["errors"] = state.get("errors", []) + ["No design plan to deploy"]
        await _mark_deploy_failure(state, "No design plan to deploy")
        return state

    try:
        from wren_agent.tools.ds_api import (
            APIError,
            DolphinSchedulerError,
            TaskDefinition,
            build_sql_task,
            get_ds_api,
        )
    except ImportError as e:
        msg = (
            f"DS deployment blocked: {e}. "
            f"Install the missing package with: uv pip install cli-anything-dolphinscheduler"
        )
        state["errors"] = state.get("errors", []) + [msg]
        await _mark_deploy_failure(state, msg)
        return state

    ds = get_ds_api()

    project_name = f"wren_{state['task_id'][:8]}"
    created_dags: list[str] = []
    ds_sources: list[dict] = []

    try:
        # ── Step 1: Create DS project (idempotent) ────────────
        project = await ds.create_or_get_project(
            project_name,
            f"Wren modeling task: {state['modeling_goal'][:100]}",
        )
        project_code = int(project["code"])

        # ── Step 2: Register data sources ─────────────────────
        # 2a. Target Doris data source (pre-created in DS — look it up;
        #     its id is required for SQL tasks in the DW workflow)
        doris_ds_id = await ds.find_datasource_id("doris_target")
        ds_sources.append({
            "name": "doris_target", "type": "DORIS",
            "pre_created": True, "id": doris_ds_id,
        })
        if doris_ds_id is None:
            state["warnings"] = state.get("warnings", []) + [
                "DS datasource 'doris_target' not found — DW SQL tasks will be skipped"
            ]

        # Business mode: each physical layer has its own selected database.
        business_layer_datasource: dict[str, int] = {}
        if state.get("source_mode", "business_db") == "business_db":
            from wren_agent.tools.pg_store import get_source_db_credentials
            target = await get_source_db_credentials(state.get("warehouse_source_id", ""))
            for layer, database in (state.get("landing") or {}).items():
                if layer not in {"ods", "dim", "dwd"} or not database or not target:
                    continue
                ds_name = _warehouse_datasource_name(layer.upper(), database)
                ds_id = await ds.find_datasource_id(ds_name)
                if ds_id is None:
                    created = await ds.create_datasource({
                        "type": "DORIS", "name": ds_name,
                        "host": target["host"], "port": target["port"],
                        "database": database, "userName": target["user"],
                        "password": target["password"],
                        "note": f"Business modeling {layer.upper()} landing",
                        "other": {},
                    })
                    ds_id = int(created.get("id")) if isinstance(created, dict) else None
                if ds_id:
                    business_layer_datasource[layer.upper()] = ds_id

        # 2b. Source data sources using Secret Manager
        source_dbs = state.get("source_databases", [])
        for sdb in source_dbs:
            if hasattr(sdb, "model_dump"):
                sdb = sdb.model_dump()

            # Get credentials from Secret Manager
            # creds = await get_secret_manager().get_db_credentials(sdb["id"])
            # For now: placeholder — in production, Secret Manager provides creds
            ds_name = f"src_{sdb.get('name', 'db')}"
            existing_id = await ds.find_datasource_id(ds_name)
            if existing_id is not None:
                ds_sources.append({"name": ds_name, "id": existing_id, "existing": True})
                continue
            landing_ods = str((state.get("landing") or {}).get("ods") or "")
            src_database = (
                landing_ods
                if str(sdb.get("db_type", "")).lower() == "doris" and landing_ods
                else sdb.get("database", "")
            )
            try:
                created = await ds.create_datasource({
                    "type": sdb.get("db_type", "mysql").upper(),
                    "name": ds_name,
                    "host": sdb.get("host", ""),
                    "port": sdb.get("port", 3306),
                    "database": src_database,
                    "userName": "",  # from Secret Manager
                    "password": "",  # from Secret Manager
                    "note": f"Source DB for task {state['task_id']}",
                    "other": {},
                })
                ds_sources.append({"name": ds_name, "result": created})
            except DolphinSchedulerError as e:
                # DS validates connectivity on create; placeholder creds fail.
                # Non-fatal: DataX tasks use runtime ${...} placeholders anyway.
                ds_sources.append({"name": ds_name, "error": str(e)})
                state["warnings"] = state.get("warnings", []) + [
                    f"DS datasource '{ds_name}' not created: {str(e)[:150]}"
                ]

        # ── Step 3: Create ODS workflows ──────────────────────
        # 3a. Execute ODS DDL first. DataX writes into these tables and the
        #     ODS layer has no other execution point (the DW workflow starts
        #     at DIM) — without this the tables never exist in Doris.
        ods_ddls = [
            a for a in (plan.sql_artifacts or [])
            if a.layer.upper() == "ODS" and a.sql_type.upper() == "DDL"
        ]
        all_ods_names, ods_to_create = _ods_sync_targets_for_deploy(state, plan)
        if state.get("execution_results") and len(ods_to_create) < len(all_ods_names):
            logger.info(
                "[%s] redeploying %d/%d ODS DAG(s); skipping tables that already succeeded",
                str(state.get("task_id", ""))[:8],
                len(ods_to_create),
                len(all_ods_names),
            )
            retry_tables = {ods.ods_table for ods in ods_to_create}
            ods_ddls = [a for a in ods_ddls if a.target_table in retry_tables]
        if ods_ddls:
            from wren_agent.nodes.design_layers import _normalize_ddl_properties
            from wren_agent.tools.doris_client import DorisClient
            ods_database = str((state.get("landing") or {}).get("ods") or "")
            # Explicit "" — do not fall back to settings.doris.database
            # (or the warehouse registry default), which may not exist.
            doris = DorisClient(database=ods_database)
            for art in ods_ddls:
                try:
                    await doris.execute_ddl(_normalize_ddl_properties(art.sql))
                except Exception as e:
                    state["warnings"] = state.get("warnings", []) + [
                        f"ODS DDL for '{art.target_table}' failed: {str(e)[:150]}"
                    ]

        for ods in ods_to_create:
            dag_name = f"{ods.ods_table}_sync"
            col_names = [f["name"] for f in (ods.fields or []) if not f.get("name", "").startswith("_ods_")]

            # Resolve source credentials for the ${reader_*} / ${writer_*}
            # runtime placeholders. Without these global params the DataX /
            # Flink tasks are guaranteed to fail on the DS worker, so a DAG
            # whose credentials can't be resolved is skipped, not deployed.
            try:
                from wren_agent.tools.secret_manager import get_credential_provider
                reader_creds = await get_credential_provider().get_credentials(
                    ods.source_db_id
                )
            except Exception as e:  # noqa: BLE001 — never deploy a doomed DAG
                state["warnings"] = state.get("warnings", []) + [
                    f"DAG '{dag_name}' skipped: source credentials unavailable "
                    f"for '{ods.source_db_id}': {str(e)[:150]}"
                ]
                continue
            sync_global_params = _build_sync_global_params(
                reader_creds, str((state.get("landing") or {}).get("ods") or ""),
            )

            if ods.sync_method == "datax":
                datax_json = _generate_datax_json(
                    source_table=ods.source_table,
                    target_table=ods.ods_table,
                    columns=col_names,
                    source_db_type=_get_source_db_type(state, ods.source_db_id),
                    incremental_column=ods.incremental_column,
                )
                # Sandbox validate DataX JSON
                await _sandbox_validate_datax(state, ods.ods_table, datax_json)

                task = TaskDefinition(
                    name=f"{dag_name}_datax_task",
                    task_type="DATAX",
                    task_params={
                        "customConfig": 1,
                        "json": datax_json,
                        "xms": 1,
                        "xmx": 1,
                    },
                )
                description = (
                    f"DataX sync: {ods.source_table} → {ods.ods_table}"
                    + (f" (incremental on {ods.incremental_column})" if ods.incremental_column else " (full)")
                )
            elif ods.sync_method == "flink_cdc":
                sdb = _get_source_db_info(state, ods.source_db_id)
                flink_config = _generate_flink_cdc_config(
                    source_table=ods.source_table,
                    target_table=ods.ods_table,
                    source_db_type=sdb.get("db_type", "mysql"),
                    source_host=sdb.get("host", ""),
                    source_port=sdb.get("port", 3306),
                    source_database=sdb.get("database", ""),
                    columns=col_names,
                    startup_mode="initial",
                )
                task = TaskDefinition(
                    name=f"{dag_name}_flink_task",
                    task_type="SHELL",
                    task_params={"rawScript": _flink_cdc_shell_script(dag_name, flink_config)},
                )
                description = f"Flink CDC: {ods.source_table} → {ods.ods_table}"
            else:
                continue

            workflow_code = await ds.create_workflow(
                project_code, dag_name, [task], [(None, task.name)],
                description=description,
                global_params=sync_global_params,
            )
            await ds.release_workflow(project_code, workflow_code, online=True)
            await ds.create_schedule_online(
                project_code, workflow_code, _quartz_cron(ods.schedule_frequency)
            )
            created_dags.append(dag_name)
            await _record_dag_map(
                (f"{(state.get('landing') or {}).get('ods')}.{ods.ods_table}"
                 if (state.get("landing") or {}).get("ods") else ods.ods_table),
                project_code, workflow_code, state["task_id"]
            )

        # Preserve already-successful ODS names in ds_dags. `created_dags` so
        # far is only this round's DS creates; union with the prior list in
        # plan order so execute_tasks still sees the full catalog.
        newly_created = set(created_dags)
        prior_dags = set(state.get("ds_dags") or [])
        created_dags = [
            n for n in all_ods_names if n in newly_created or n in prior_dags
        ]
        created_dags.extend(n for n in newly_created if n not in set(created_dags))

        # ── Step 4: Create DW layer workflow (DIM → DWD → DWS → ADS) ──
        dw_artifacts = [
            a for a in (plan.sql_artifacts or [])
            if a.layer.upper() in ("DIM", "DWD", "DWS", "ADS")
        ]

        # Warehouse mode (T-11): DWS/ADS run against their own layer
        # databases (per-layer DS datasources) and the workflow starts with
        # DEPENDENT tasks waiting on the already-deployed upstream DWD/DWM
        # DAGs; the schedule inherits the first upstream workflow's cron.
        is_warehouse = state.get("source_mode") == "warehouse"
        layer_datasource: dict[str, int] = dict(business_layer_datasource)
        upstream_refs: list[tuple[str, str, str]] = []
        schedule_cron = "0 0 1 * * ? *"
        if is_warehouse:
            wh_layers = _warehouse_layers(state)
            from wren_agent.config import settings as wsettings
            for layer, db in (("DWS", wh_layers.get("dws")), ("ADS", wh_layers.get("ads"))):
                if not db:
                    continue
                # Datasource name carries the landing database: the old shared
                # `doris_dws`/`doris_ads` names were reused across tasks with
                # DIFFERENT landing choices, so DWS/ADS tables were created in
                # the FIRST task's database while load SQL referenced the
                # current landing db → "Table does not exist" at execution
                # (task 67b6907f: DWS tables landed in db_dws but ADS read
                # dws_ecommerce_orders_sh.*). Per-(layer, db) names give each
                # landing its own datasource and never mutate shared ones.
                ds_name = _warehouse_datasource_name(layer, db)
                ds_id = await ds.find_datasource_id(ds_name)
                if ds_id is None:
                    try:
                        created = await ds.create_datasource({
                            "type": "DORIS",
                            "name": ds_name,
                            "host": wsettings.doris.host,
                            "port": wsettings.doris.query_port,
                            "database": db,
                            "userName": wsettings.doris.user,
                            "password": wsettings.doris.password,
                            "note": f"Warehouse {layer} layer (wayfinder T-11)",
                            "other": {},
                        })
                        ds_id = int(created.get("id")) if isinstance(created, dict) else None
                    except DolphinSchedulerError as e:
                        state["warnings"] = state.get("warnings", []) + [
                            f"DS datasource '{ds_name}' not created: {str(e)[:150]}"
                        ]
                if ds_id:
                    layer_datasource[layer] = ds_id
            upstream_refs = await _resolve_warehouse_upstreams(ds, state, plan)
            if upstream_refs:
                inherited = await _resolve_upstream_schedule(ds, upstream_refs[0])
                if inherited:
                    schedule_cron = inherited
                    state["warnings"] = state.get("warnings", []) + [
                        f"[DAG] schedule inherited from upstream '{upstream_refs[0][0]}': {inherited}"
                    ]

        dw_dag_name = f"dw_transform_{state['task_id'][:8]}"
        skip_dw = False
        if state.get("execution_results"):
            dw_pending = _unsuccessful_or_pending_dags(
                [dw_dag_name], state.get("execution_results") or [],
            )
            skip_dw = dw_dag_name not in dw_pending
            if skip_dw and dw_dag_name in (state.get("ds_dags") or []) and dw_dag_name not in created_dags:
                created_dags.append(dw_dag_name)

        if dw_artifacts and (doris_ds_id is not None or layer_datasource) and not skip_dw:
            task_defs: list[TaskDefinition] = []
            relations: list[tuple[str | None, str]] = []
            prev_layer_names: list[str] = []

            # Warehouse mode: DEPENDENT tasks wait on upstream workflows
            if is_warehouse and upstream_refs:
                from cli_anything.dolphinscheduler.core.tasks import build_dependent_task
                dep_names: list[str] = []
                for i, (table, pcode, wcode) in enumerate(upstream_refs):
                    # DEPENDENT task names must be bare identifiers — the
                    # qualified table name (`db.table`) carries a dot that DS
                    # rejects with "parameter invalid", and the 60-char cut
                    # made sibling tables collide into a "duplicate error"
                    # (tasks 63ce2059 / 38ea6865). Bare name + index suffix.
                    bare = str(table).split(".", 1)[-1].strip("`")
                    dep_name = f"wait_upstream_{bare}"[:55] + f"_{i}"
                    task_defs.append(build_dependent_task(
                        name=dep_name,
                        depend_items=[{
                            "depTaskType": "DEP_WORKFLOW",
                            "projectCode": int(pcode),
                            "definitionCode": int(wcode),
                            "depTaskCode": 0,
                            "depCycle": "DAY",
                            "dateValue": "today",
                        }],
                        description=f"Wait for deployed workflow of {table}",
                    ))
                    relations.append((None, dep_name))
                    dep_names.append(dep_name)
                prev_layer_names = dep_names

            for layer in ("DIM", "DWD", "DWS", "ADS"):
                layer_arts = [a for a in dw_artifacts if a.layer.upper() == layer]
                if not layer_arts:
                    continue
                # DDL before DML within a layer
                layer_arts.sort(key=lambda a: 0 if a.sql_type.upper() == "DDL" else 1)
                layer_names: list[str] = []
                ddl_names: list[str] = []
                for i, art in enumerate(layer_arts):
                    tname = f"sql_{layer.lower()}_{i}_{art.target_table}"[:60]
                    # Normalize DDL at the deploy boundary too — the sandbox
                    # normalize pass is best-effort and may be skipped
                    # (budget/连接失败), so never rely on it having run.
                    art_sql = art.sql
                    if art.sql_type.upper() == "DDL":
                        from wren_agent.nodes.design_layers import _normalize_ddl_properties
                        art_sql = _normalize_ddl_properties(art_sql)
                    task_defs.append(build_sql_task(
                        name=tname,
                        sql=art_sql,
                        datasource=layer_datasource.get(layer, doris_ds_id),
                        # Must match the registered DS datasource type exactly —
                        # declaring MYSQL against a DORIS datasource makes the
                        # worker fail at init (task dies with no log produced).
                        datasource_type="DORIS",
                        sql_type="1",             # non-query
                        description=art.description[:200],
                    ))
                    is_ddl = art.sql_type.upper() == "DDL"
                    upstream = prev_layer_names if is_ddl or not ddl_names else ddl_names
                    if upstream:
                        for up in upstream:
                            relations.append((up, tname))
                    else:
                        relations.append((None, tname))
                    layer_names.append(tname)
                    if is_ddl:
                        ddl_names.append(tname)
                prev_layer_names = layer_names

            workflow_code = await ds.create_workflow(
                project_code, dw_dag_name, task_defs, relations,
                description="DW layer transformation DAG (DIM → DWD → DWS → ADS)",
            )
            await ds.release_workflow(project_code, workflow_code, online=True)
            await ds.create_schedule_online(project_code, workflow_code, schedule_cron)
            created_dags.append(dw_dag_name)

            # Record table -> workflow mapping for every deployed DW artifact
            # (all modes — warehouse mode depends on it, T-11)
            for art in dw_artifacts:
                if art.target_table:
                    await _record_dag_map(
                        art.target_table, project_code, workflow_code, state["task_id"]
                    )
        elif dw_artifacts:
            state["warnings"] = state.get("warnings", []) + [
                f"DW workflow skipped: {len(dw_artifacts)} SQL artifacts but no 'doris_target' datasource in DS"
            ]

        # ── Step 5: Update state ──────────────────────────────
        state["ds_project_name"] = project_name
        state["ds_project_code"] = project_code
        state["ds_data_sources"] = ds_sources
        state["ds_dags"] = created_dags
        state["deploy_succeeded"] = True
        state["deploy_failure_count"] = 0

        from wren_agent.tools.event_emitter import emit_phase, emit_dag_deploy
        emit_phase(state, "dag_deploy")
        for dag in created_dags:
            emit_dag_deploy(state, dag, "deployed")

        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "dag_deploy",
            "action": "deployed",
            "project": project_name,
            "project_code": project_code,
            "data_sources": len(ds_sources),
            "dags": len(created_dags),
            "dag_names": created_dags,
            "timestamp": now.isoformat(),
        }]

        return state

    except DolphinSchedulerError as e:
        # Typed DS failure — str(e) carries the real server-side message
        msg = f"DS deployment failed ({type(e).__name__}): {e}"
        state["errors"] = state.get("errors", []) + [msg]
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "dag_deploy",
            "action": "failed",
            "error": msg,
            "timestamp": now.isoformat(),
        }]
        await _mark_deploy_failure(state, msg)
        return state
    except Exception as e:
        state["errors"] = state.get("errors", []) + [f"DS deployment failed: {e}"]
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "dag_deploy",
            "action": "failed",
            "error": str(e),
            "timestamp": now.isoformat(),
        }]
        await _mark_deploy_failure(state, f"DS deployment failed: {e}")
        return state
    finally:
        ds.close()


def _is_ods_sync_dag(dag_name: str) -> bool:
    """ODS DataX/CDC workflows are named `{ods_table}_sync` (see dag_deploy)."""
    return str(dag_name).endswith("_sync")


def _execution_result_dag_name(result: ExecutionResult) -> str | None:
    rows = getattr(result, "rows_written", None) or {}
    if rows:
        return str(next(iter(rows)))
    tid = str(getattr(result, "task_instance_id", "") or "")
    for prefix in ("not_found_", "start_failed_", "error_"):
        if tid.startswith(prefix):
            return tid[len(prefix):]
    return None


def _unsuccessful_or_pending_dags(
    ds_dags: list[str],
    results: list[ExecutionResult] | None,
) -> list[str]:
    """DAGs that have not succeeded yet — failures plus names never executed.

    Used so a post-execute dag_deploy / execute_tasks round only touches the
    tables that actually failed (task 9af6aa77 re-created all 410 workflows
    after 7 DataX failures). Unnameable failures fall back to the full list
    so a retry cannot silently skip work.
    """
    dags = [str(n) for n in ds_dags if n]
    if not results:
        return list(dags)
    by_name: dict[str, ExecutionResult] = {}
    unnamed_failure = False
    for result in results:
        name = _execution_result_dag_name(result)
        if name:
            by_name[name] = result
        elif not getattr(result, "success", False):
            unnamed_failure = True
    if unnamed_failure:
        return list(dags)
    return [n for n in dags if n not in by_name or not by_name[n].success]


def _ods_sync_targets_for_deploy(state: AgentState, plan: Any) -> tuple[list[str], list[Any]]:
    """All intended ODS dag names, and the designs that still need a DS create."""
    designs = list(getattr(plan, "ods_designs", None) or [])
    all_names = [f"{ods.ods_table}_sync" for ods in designs]
    pending = set(_unsuccessful_or_pending_dags(all_names, state.get("execution_results") or []))
    if not state.get("execution_results"):
        return all_names, designs
    return all_names, [ods for ods in designs if f"{ods.ods_table}_sync" in pending]


def _ods_execute_concurrency() -> int:
    from wren_agent.config import settings
    return int(settings.agent.execute_concurrency)


def _failed_execution(
    instance_id: str,
    summary: str,
    raw_log: str,
    error_type: ErrorType,
    dag_name: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_instance_id=instance_id,
        success=False,
        error=ErrorClassification(
            task_instance_id=instance_id,
            error_type=error_type,
            raw_log=raw_log,
            summary=summary,
            action="escalate",
        ),
        rows_written={dag_name: 0} if dag_name else {},
    )


async def _collect_instance_logs(ds, project_code: int, instance_id: int) -> list[str]:
    """FAILED tasks first, full paginated log; otherwise a short window.

    The old ``task_list[:10]`` slice skipped late failures and the 100-line
    log window hid deep errors, so classify_errors saw an empty log and
    defaulted to LOGIC → blind repair rounds (task a1af4bed: task #18/19
    died with ``Table name length exceeds limit`` beyond line 100).
    """
    from wren_agent.tools.ds_api import DolphinSchedulerError

    task_logs: list[str] = []
    try:
        task_list = await ds.get_instance_tasks(int(project_code), instance_id)
        failed_tis = [
            t for t in task_list
            if str((t.get("state") if isinstance(t, dict) else "") or "").upper()
            in ("FAILURE", "FAILED")
        ]
        order = failed_tis if failed_tis else task_list
        for ti in order[:8]:
            ti_id = ti.get("id") if isinstance(ti, dict) else ti
            if not ti_id:
                continue
            try:
                if ti in failed_tis:
                    log_text = await _fetch_full_task_log(ds, int(ti_id))
                else:
                    log_text = await ds.query_task_log(int(ti_id))
            except DolphinSchedulerError as e:
                log_text = f"[log fetch failed] {e}"
            if log_text:
                task_logs.append(log_text)
    except DolphinSchedulerError as e:
        task_logs.append(f"[log fetch failed] {e}")
    return task_logs


async def _execute_one_dag(
    ds,
    project_code: int,
    dag_name: str,
    state: AgentState,
    started_at: datetime,
) -> ExecutionResult:
    """Start one workflow, poll until done, collect logs."""
    from wren_agent.tools.ds_api import DolphinSchedulerError, NotFoundError

    try:
        try:
            workflow_code = await ds.resolve_workflow_code(int(project_code), dag_name)
        except NotFoundError:
            return _failed_execution(
                f"not_found_{dag_name}",
                "Workflow definition not found in DS",
                f"Workflow definition not found: {dag_name}",
                ErrorType.CONFIG,
                dag_name=dag_name,
            )

        instance_id = await ds.start_workflow(
            int(project_code), workflow_code,
            start_params=_ods_bootstrap_start_params(
                dag_name, state.get("design_plan"),
            ),
        )
        if instance_id is None:
            return _failed_execution(
                f"start_failed_{dag_name}",
                f"Failed to start workflow: {dag_name}",
                f"DS returned no instance id for {dag_name}",
                ErrorType.CONFIG,
                dag_name=dag_name,
            )

        success, logs, _instance_data = await _poll_workflow_completion(
            ds, int(project_code), instance_id
        )
        task_logs = await _collect_instance_logs(ds, int(project_code), instance_id)
        return ExecutionResult(
            task_instance_id=str(instance_id),
            success=success,
            start_time=started_at,
            end_time=datetime.now(timezone.utc),
            logs=task_logs if task_logs else logs,
            rows_written={dag_name: 0},
        )
    except DolphinSchedulerError as e:
        return _failed_execution(
            f"error_{dag_name}",
            f"DS error executing {dag_name}: {str(e)[:120]}",
            str(e),
            ErrorType.CONFIG,
            dag_name=dag_name,
        )
    except Exception as e:
        return _failed_execution(
            f"error_{dag_name}",
            f"Unexpected error executing {dag_name}",
            str(e),
            ErrorType.LOGIC,
            dag_name=dag_name,
        )


async def execute_tasks(state: AgentState) -> AgentState:
    """Execute all deployed DAGs on Dolphin Scheduler via cli-anything core.

    For each DAG:
    1. Resolve workflow code by name and start a workflow instance
    2. Poll for completion (success or failure)
    3. Collect task-instance logs
    4. Record ExecutionResult

    Runs DAGs in dependency order:
    ODS `_sync` workflows in parallel (capped by AGENT_EXECUTE_CONCURRENCY)
    → DW transform DAG sequentially after every ODS run in this round has finished.

    A later round (after classify_errors → design_revise → dag_deploy) only
    starts DAGs that have not yet succeeded, and merges those results into
    the previous execution_results so successful tables are not re-run.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.EXECUTE

    try:
        from wren_agent.tools.ds_api import get_ds_api
    except ImportError as e:
        msg = f"DS execution blocked: {e}. Install: uv pip install cli-anything-dolphinscheduler"
        state["errors"] = state.get("errors", []) + [msg]
        return state

    ds = get_ds_api()

    ds_dags = list(state.get("ds_dags", []) or [])
    project_code = state.get("ds_project_code")
    if not project_code:
        state["errors"] = state.get("errors", []) + ["No project_code in state"]
        state["execution_results"] = [_synthetic_failure("no_project_code", "No project_code in state — deployment did not complete")]
        ds.close()
        return state

    prior_results = list(state.get("execution_results") or [])
    to_run = _unsuccessful_or_pending_dags(ds_dags, prior_results)
    results_by_name: dict[str, ExecutionResult] = {}
    for prior in prior_results:
        name = _execution_result_dag_name(prior)
        if name:
            results_by_name[name] = prior

    try:
        ods_dags = [n for n in to_run if _is_ods_sync_dag(n)]
        other_dags = [n for n in to_run if not _is_ods_sync_dag(n)]

        if ods_dags:
            concurrency = max(1, _ods_execute_concurrency())
            logger.info(
                "[%s] executing %d ODS DAG(s) with concurrency %d",
                str(state.get("task_id", ""))[:8], len(ods_dags), concurrency,
            )
            sem = asyncio.Semaphore(concurrency)

            async def _run_ods(dag_name: str) -> ExecutionResult:
                async with sem:
                    worker = get_ds_api()
                    try:
                        return await _execute_one_dag(
                            worker, int(project_code), dag_name, state, now,
                        )
                    finally:
                        worker.close()

            gathered = await asyncio.gather(
                *[_run_ods(name) for name in ods_dags],
                return_exceptions=True,
            )
            for dag_name, item in zip(ods_dags, gathered):
                if isinstance(item, BaseException):
                    results_by_name[dag_name] = _failed_execution(
                        f"error_{dag_name}",
                        f"Unexpected error executing {dag_name}",
                        str(item),
                        ErrorType.LOGIC,
                        dag_name=dag_name,
                    )
                else:
                    results_by_name[dag_name] = item

        for dag_name in other_dags:
            results_by_name[dag_name] = await _execute_one_dag(
                ds, int(project_code), dag_name, state, now,
            )
    finally:
        ds.close()

    state["execution_results"] = [results_by_name[name] for name in ds_dags if name in results_by_name]

    from wren_agent.tools.doris_dml_repair import record_pending_repair_outcome
    results = state["execution_results"]
    if results and all(r.success for r in results):
        record_pending_repair_outcome(state, True)

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "execute")

    return state


async def _fetch_full_task_log(ds, task_instance_id: int, chunk: int = 200, max_chunks: int = 25) -> str:
    """Fetch a task-instance log in paginated chunks until exhausted.

    DS's log API truncates at `limit` lines (default 100) — on long SQLs the
    real error lands past the window and classification fell back to a LOGIC
    catch-all (task a1af4bed: `Table name length exceeds limit` sat beyond
    line 100 → blind revision rounds). Paginating to the end fixes the feed
    that classify_errors and design_revise rely on.
    """
    from wren_agent.tools.ds_api import DolphinSchedulerError

    parts: list[str] = []
    skip = 0
    for _ in range(max_chunks):
        try:
            chunk_text = await ds.query_task_log(
                task_instance_id, skip_line_num=skip, limit=chunk
            )
        except DolphinSchedulerError as e:
            parts.append(f"[log fetch failed] {e}")
            break
        if not chunk_text:
            break
        parts.append(chunk_text)
        lines = chunk_text.splitlines()
        skip += len(lines)
        if len(lines) < chunk:
            break
    return "\n".join(parts)


async def _poll_workflow_completion(
    ds,
    project_code: int,
    instance_id: int,
    max_polls: int | None = None,
    poll_interval: int | None = None,
) -> tuple[bool, list[str], dict]:
    """Poll a DS workflow instance until completion.

    Returns (success, logs, instance_data).
    """
    from wren_agent.config import settings
    from wren_agent.tools.ds_api import DolphinSchedulerError

    max_polls = max_polls or settings.ds.max_poll_cycles
    poll_interval = poll_interval or settings.ds.poll_interval

    logs: list[str] = []
    for _ in range(max_polls):
        try:
            data = await ds.get_workflow_instance(project_code, instance_id)
        except DolphinSchedulerError as e:
            return False, [f"Error polling: {e}"], {}

        state_val = str((data or {}).get("state", "")).upper()

        if state_val in ("SUCCESS", "FAILURE", "STOP", "KILL"):
            success = state_val == "SUCCESS"
            return success, logs, data

        logs.append(f"[POLL] State: {state_val}")
        await asyncio.sleep(poll_interval)

    return False, logs + [f"Timeout after {max_polls * poll_interval}s"], {"state": "TIMEOUT"}


_INSERT_TARGET_FROM_LOG_RE = re.compile(
    r"\bINSERT\s+INTO\s+(?:`?([\w$-]+)`?\.)?`?([\w$-]+)`?\s*"
    r"(?:\(|OVERWRITE|SELECT|VALUES)",
    re.I,
)
_NOT_FOUND_COLUMN_FROM_LOG_RE = re.compile(
    r"column\s+(?:`?([\w$-]+)`?)\s+is\s+not\s+found",
    re.I,
)


def _insert_target_table_from_log(raw_log: str) -> tuple[str, str] | None:
    """Extract (database, table) of the INSERT a failing SQL targeted.

    Parses the first `INSERT INTO [db.]table` in the worker log. The db
    qualifier is what matters — Doris's own error text reports the bare
    table name (`column X is not found in table dws_...`), so the qualifier
    must come from the statement itself (task 3034d2f8). Returns None when
    no qualifier/table can be parsed; the caller then keeps the original
    classification instead of guessing.
    """
    m = _INSERT_TARGET_FROM_LOG_RE.search(raw_log or "")
    if not m:
        return None
    db = (m.group(1) or "").strip("`")
    table = (m.group(2) or "").strip("`")
    if not db or not table:
        return None
    return db, table


def _not_found_column_from_log(raw_log: str) -> str | None:
    """Extract the missing column name from `column X is not found`."""
    m = _NOT_FOUND_COLUMN_FROM_LOG_RE.search(raw_log or "")
    return (m.group(1) or "").strip("`").lower() if m else None


async def _verify_column_not_found_against_real_schema(
    raw_log: str,
    classification: ErrorClassification,
    state: AgentState,
) -> ErrorClassification:
    """Re-check a DEPENDENCY classification against the REAL Doris schema.

    `column X is not found in table Y` is ambiguous: it is an upstream
    schema change only when the column existed before and was removed. When
    the target table already exists (an earlier run of the same goal built a
    different schema) and the missing column is simply absent from it, the
    failure is the agent's own SQL vs an existing table — repairable via
    design_revise (LOGIC), not an escalate. Doris reports the bare table
    name, so the INSERT qualifier from the log resolves the database (task
    3034d2f8: no-op CREATE TABLE IF NOT EXISTS + missing monthly_charge_count
    was mis-escalated to human). Degrades to the original classification on
    any resolution/connectivity failure.
    """
    if classification.error_type != ErrorType.DEPENDENCY:
        return classification
    missing_col = _not_found_column_from_log(raw_log)
    target = _insert_target_table_from_log(raw_log)
    if not missing_col or not target:
        return classification
    db, table = target

    from wren_agent.tools.doris_client import DorisClient
    client = DorisClient(database=db)
    try:
        if not await client.table_exists(table):
            return classification  # target missing → true dependency
        real = await client.get_table_schema(table)
    except Exception:  # noqa: BLE001 — Doris unreachable: keep original
        return classification

    real_cols = {str(c["name"]).lower() for c in real.get("columns", [])}
    if missing_col in real_cols:
        return classification  # column exists in the real table — upstream change
    # The target table exists and lacks the referenced column: the agent's
    # DML targets a column the existing schema does not have. Reclassify as
    # LOGIC so design_revise reconciles the SQL instead of escalating.
    return ErrorClassification(
        task_instance_id=classification.task_instance_id,
        error_type=ErrorType.LOGIC,
        raw_log=raw_log,
        summary=(
            "SQL references column "
            f"{missing_col} that does not exist in existing table "
            f"{db}.{table} (real columns: {sorted(real_cols)}) — reconcile "
            "the INSERT/DDL with the existing schema"
        ),
        retry_count=classification.retry_count,
        action="escalate",
        details=(
            "Reclassified from dependency by real-schema verification: the "
            "target table already exists and lacks the referenced column, so "
            "this is the agent's own SQL vs an existing table, not an "
            "upstream schema change."
        ),
        fix_suggestion=(
            "Rename the target table (append _v2) so the DDL creates a fresh "
            "schema, or drop/rename the phantom column in the INSERT list and "
            "SELECT aliases to match the existing table."
        ),
        llm_classified=classification.llm_classified,
    )


async def classify_errors(state: AgentState) -> AgentState:
    """Classify execution failures into error types.

    Parses Dolphin Scheduler task logs to determine root cause.
    Categorizes into: transient | resource | data | logic | config | dependency.
    """
    from wren_agent.graph import MAX_DESIGN_REVISIONS
    from wren_agent.models.task import TaskStatus
    from wren_agent.tools.dolphin_scheduler import (
        classify_execution_error,
        classify_execution_error_with_llm,
        reconcile_llm_and_keyword_classification,
    )

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.ERROR_HANDLING

    execution_results = state.get("execution_results", [])
    classified: list[ErrorClassification] = []

    for result in execution_results:
        if not result.success:
            # Early-exit / synthetic failures (no_project_code, not_found_*,
            # start_failed_*) carry a precise classification already — reuse it
            # instead of re-deriving from empty logs (which falls through to
            # the LOGIC catch-all and mis-routes back to redeploy).
            if result.error is not None:
                classified.append(result.error)
                continue
            raw_log = "\n".join(result.logs) if result.logs else "No logs available"
            # LLM classification first (key error excerpt, high-quality
            # summary/detail/fix); keyword classifier as fallback when the
            # LLM call fails, times out, or returns an invalid answer.
            # Always keep the keyword result: LLM DEPENDENCY must not
            # override agent-SQL unknown-column (task de018b42).
            keyword = classify_execution_error(
                task_instance_id=result.task_instance_id,
                raw_log=raw_log,
                retry_count=state.get("retry_count", 0),
            )
            llm_cls = await classify_execution_error_with_llm(
                task_instance_id=result.task_instance_id,
                raw_log=raw_log,
                retry_count=state.get("retry_count", 0),
            )
            classification = reconcile_llm_and_keyword_classification(
                llm_cls, keyword,
            )
            # `column X is not found in table Y` alone cannot distinguish an
            # upstream schema change from the agent's own SQL targeting an
            # existing table (task 3034d2f8). Verify against the real Doris
            # schema before trusting a DEPENDENCY/escalate.
            if classification.error_type == ErrorType.DEPENDENCY:
                classification = await _verify_column_not_found_against_real_schema(
                    raw_log, classification, state,
                )
            classified.append(classification)

    from wren_agent.tools.doris_dml_repair import record_pending_repair_outcome
    if classified and any(e.error_type.value in ("logic", "data") for e in classified):
        record_pending_repair_outcome(state, False)

    state["error_classifications"] = classified

    # Repairable SQL errors with no revision budget left: fail here so the
    # graph goes to generate_report instead of human_approval → dag_deploy
    # with the same broken statements.
    repairable = [
        e for e in classified if e.error_type.value in ("data", "logic")
    ]
    if repairable and state.get("design_revision_count", 0) >= MAX_DESIGN_REVISIONS:
        msg = (
            "SQL execution errors remain after design revision budget exhausted "
            f"({MAX_DESIGN_REVISIONS} rounds) — refusing to redeploy unrepaired SQL. "
            f"Last error: {repairable[0].summary}"
        )
        if msg not in state.get("errors", []):
            state["errors"] = state.get("errors", []) + [msg]
        state["status"] = TaskStatus.FAILED
        state["failure_kind"] = "execution"

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "classify_errors",
        "action": "classified",
        "error_count": len(classified),
        "error_types": [e.error_type.value for e in classified],
        "timestamp": now.isoformat(),
    }]

    return state


async def retry_execution(state: AgentState) -> AgentState:
    """Retry failed executions (transient errors only).

    Uses exponential backoff before retrying:
    - Attempt 1: wait 30s
    - Attempt 2: wait 60s
    - Attempt 3: wait 120s
    """
    retry_count = state.get("retry_count", 0) + 1
    state["retry_count"] = retry_count

    # Exponential backoff
    delay_seconds = 30 * (2 ** (retry_count - 1))
    await asyncio.sleep(delay_seconds)

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "retry_execution",
        "action": "retrying",
        "attempt": retry_count,
        "max_retries": state.get("max_retries", 3),
        "backoff_seconds": delay_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]

    return state


async def error_handling(state: AgentState) -> AgentState:
    """Handle unrecoverable errors after execution."""
    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    errors = state.get("error_classifications", [])
    fatal = [e for e in errors if e.error_type.value != "transient"]

    # LLM-classified errors carry details; append them so the dashboard
    # "technical details" expander shows the real failure, not just the
    # one-line summary. Keyword-classified errors stay single-line.
    state["errors"] = state.get("errors", []) + [
        f"Fatal error [{e.error_type.value}]: {e.summary}"
        + (f"\n  Details: {e.details[:300]}" if e.details else "")
        for e in fatal
    ]

    # Loop guard: error_handling may route to human_approval and auto mode
    # auto-approves, so any failure that produces no execution-error
    # approval item (e.g. final validation failure) cycles
    # error → approve → deploy → execute → validate → error forever
    # (observed on task a0a63385: 355 checkpoints in 20 min). The
    # approval-side guard only counts rounds when an execution approval item
    # exists; cap here instead — every such loop passes through this node. The
    # graph terminates immediately once this guard marks the task FAILED.
    rounds = state.get("error_recovery_rounds", 0) + 1
    state["error_recovery_rounds"] = rounds
    if rounds >= 3:
        from wren_agent.models.task import TaskStatus
        msg = (
            f"Error recovery exhausted after {rounds} rounds — failing the task "
            "instead of looping error → approval → deploy → execute → validate"
        )
        state["errors"] = state.get("errors", []) + [msg]
        state["status"] = TaskStatus.FAILED

    return state


# ── Source DB info helpers ──────────────────────────────────────


def _get_source_db_info(state, db_id: str) -> dict:
    """Get source database connection info from state."""
    source_dbs = state.get("source_databases", [])
    for sdb in source_dbs:
        s = {}
        if hasattr(sdb, "model_dump"):
            s = sdb.model_dump()
        elif isinstance(sdb, dict):
            s = sdb
        else:
            s = sdb.__dict__

        sid = s.get("id") or s.get("name", "")
        if sid == db_id:
            return s
    return {}


def _get_source_db_type(state, db_id: str) -> str:
    """Get source database type."""
    return _get_source_db_info(state, db_id).get("db_type", "mysql")


# ── Drift fix SQL generators ────────────────────────────────────


def _generate_column_removal_fix(table_name: str, column: str) -> str:
    """Generate ALTER TABLE DROP COLUMN for DW layers."""
    return (
        f"-- Auto-generated fix for schema drift\n"
        f"-- Column '{column}' was removed from source table '{table_name}'\n"
        f"-- Please verify this does not break downstream logic\n\n"
        f"ALTER TABLE ods_{table_name} DROP COLUMN IF EXISTS {column};\n"
    )


def _generate_type_change_fix(
    table_name: str,
    column: str,
    old_type: str,
    new_type: str,
) -> str:
    """Generate ALTER TABLE MODIFY COLUMN for type changes."""
    return (
        f"-- Auto-generated fix for schema drift\n"
        f"-- Column '{column}' type changed: {old_type} → {new_type}\n"
        f"-- Please verify compatibility before applying\n\n"
        f"ALTER TABLE ods_{table_name} MODIFY COLUMN {column} {new_type};\n"
    )


# ── DataX JSON generator (production-grade) ──────────────────────


def _build_jdbc_url(db_type: str, host: str, port: int, database: str) -> str:
    """Build a JDBC URL for the DataX reader by source DB type."""
    t = (db_type or "mysql").lower()
    if t == "postgresql":
        return f"jdbc:postgresql://{host}:{port}/{database}"
    if t == "oracle":
        return f"jdbc:oracle:thin:@//{host}:{port}/{database}"
    if t == "dm":
        return f"jdbc:dm://{host}:{port}/{database}"
    if t == "kingbase":
        return f"jdbc:kingbase8://{host}:{port}/{database}"
    # mysql and mysql-protocol compatibles
    return (
        f"jdbc:mysql://{host}:{port}/{database}"
        "?useUnicode=true&characterEncoding=utf-8&useSSL=false"
        "&serverTimezone=Asia/Shanghai"
    )


def _build_sync_global_params(reader_creds: dict, writer_database: str = "") -> list[dict]:
    """Build DS workflow global params for the ${...} placeholders used in
    DataX JSON and Flink CDC configs.

    Scheduled runs use these workflow-level defaults. The agent's first
    ``execute_tasks`` start overrides ``last_run_time`` via startParams so
    incremental ODS jobs bootstrap a full historical load (task 5385ea71).
    ``last_run_time`` uses a DS built-in time expression (yesterday 00:00),
    matching the t+1 daily schedule.
    """
    from wren_agent.config import settings

    def p(prop: str, value: str) -> dict:
        return {"prop": prop, "value": value, "direct": "IN", "type": "VARCHAR"}

    doris = settings.doris
    return [
        p("reader_username", reader_creds.get("user", "")),
        p("reader_password", reader_creds.get("password", "")),
        p("reader_jdbc_url", _build_jdbc_url(
            reader_creds.get("db_type", "mysql"),
            reader_creds.get("host", ""),
            reader_creds.get("port", 3306),
            reader_creds.get("database", ""),
        )),
        p("writer_username", doris.user),
        p("writer_password", doris.password),
        p("writer_database", writer_database or doris.database),
        p("writer_jdbc_url", f"jdbc:mysql://{doris.host}:{doris.query_port}/{writer_database or doris.database}"),
        p("writer_load_url", f"{doris.host}:{doris.fe_http_port}"),
        p("doris_fe_url", f"{doris.host}:{doris.fe_http_port}"),
        p("last_run_time", "$[yyyy-MM-dd-1] 00:00:00"),
    ]


ODS_BOOTSTRAP_LAST_RUN_TIME = "1970-01-01 00:00:00"


def _ods_bootstrap_start_params(dag_name: str, plan: Any) -> dict[str, str] | None:
    """Return startParams that force a full first load for incremental ODS DAGs.

    DataX query SQL is ``WHERE {col} > '${last_run_time}'``. The workflow
    default expands to yesterday, so the first agent-triggered run would
    otherwise copy zero historical rows and fail final_validate with empty
    materialized ODS targets (task 5385ea71).
    """
    if not dag_name.endswith("_sync"):
        return None
    target = dag_name[: -len("_sync")]
    for ods in (getattr(plan, "ods_designs", None) or []):
        if getattr(ods, "ods_table", "") == target and getattr(ods, "incremental_column", None):
            return {"last_run_time": ODS_BOOTSTRAP_LAST_RUN_TIME}
    return None


def _generate_datax_json(
    source_table: str,
    target_table: str,
    columns: list[str] | None = None,
    source_db_type: str = "mysql",
    incremental_column: str | None = None,
    doris_load_url: str = "${writer_load_url}",
) -> str:
    """Generate a production-grade DataX JSON for source → Doris sync.

    Supports:
    - Column-level mapping (reader → writer)
    - Incremental WHERE clause
    - Speed/channel configuration
    - Error tolerance limits
    - Pre/Post SQL for Doris writer

    Placeholders ($ {...}) are substituted by Dolphin Scheduler at runtime.
    """
    import json

    reader_name = _get_datax_reader(source_db_type)

    # Column mapping
    col_list = columns or ["*"]
    reader_cols = [{"name": c, "type": "string"} if c != "*" else "*" for c in col_list]
    writer_cols = col_list if col_list != ["*"] else ["*"]

    # Build reader WHERE clause
    query_sql = f"SELECT {', '.join(col_list)} FROM {source_table}"
    if incremental_column:
        # Scheduled runs use yesterday via workflow global params. The first
        # execute_tasks start overrides last_run_time to epoch (see
        # _ods_bootstrap_start_params) so historical rows are not filtered out.
        query_sql += f" WHERE {incremental_column} > '${{last_run_time}}'"

    config = {
        "job": {
            "setting": {
                "speed": {
                    # channel-count only. A job-level "byte" cap requires a
                    # matching core.transport.channel.speed.byte override or
                    # DataX aborts ("单个channel的bps值不能为空")
                    "channel": 3,
                },
                "errorLimit": {
                    "record": 0,
                    "percentage": 0.02,
                },
            },
            "content": [{
                "reader": {
                    "name": reader_name,
                    "parameter": {
                        "username": "${reader_username}",
                        "password": "${reader_password}",
                        # querySql mode: column list lives in the SQL itself
                        # (DataX warns if a separate column config coexists)
                        "connection": [{
                            "querySql": [query_sql],
                            "jdbcUrl": ["${reader_jdbc_url}"],
                        }],
                    },
                },
                "writer": {
                    "name": "doriswriter",
                    # DataX 202309 doriswriter schema: jdbcUrl/selectedDatabase/
                    # table nest under connection[]; a flat database/table
                    # layout fails validation ("selectedDatabase 是必填参数")
                    "parameter": {
                        "username": "${writer_username}",
                        "password": "${writer_password}",
                        "loadUrl": [doris_load_url],
                        "column": writer_cols,
                        "preSql": [],
                        "postSql": [],
                        "connection": [{
                            "jdbcUrl": "${writer_jdbc_url}",
                            "selectedDatabase": "${writer_database}",
                            "table": [target_table],
                        }],
                        "loadProps": {
                            "format": "json",
                            "strip_outer_array": "true",
                            "max_filter_ratio": "0.02",
                        },
                    },
                },
                "transformer": [],
            }],
        },
    }
    return json.dumps(config, indent=2, ensure_ascii=False)


def _get_datax_reader(db_type: str) -> str:
    """Map DB type to DataX reader plugin name."""
    readers = {
        "mysql": "mysqlreader",
        "postgresql": "postgresqlreader",
        "oracle": "oraclereader",
        "dm": "dmreader",
        "kingbase": "postgresqlreader",  # Kingbase uses PG protocol
    }
    return readers.get(db_type.lower(), "mysqlreader")


# ── Flink CDC config generator (production-grade) ────────────────


def _generate_flink_cdc_config(
    source_table: str,
    target_table: str,
    source_db_type: str,
    source_host: str,
    source_port: int,
    source_database: str,
    columns: list[str] | None = None,
    server_id: str = "5400-5500",
    startup_mode: str = "initial",
    checkpoint_interval: int = 60000,
) -> str:
    """Generate a Flink CDC configuration for real-time sync.

    Supports:
    - MySQL CDC (Debezium) and Postgres CDC
    - Startup modes: initial (full snapshot + binlog) / latest-offset / timestamp
    - Checkpoint configuration
    - Column mapping with DDL sync to Doris

    Returns a JSON string suitable for the Flink task in Dolphin Scheduler.
    """
    import json

    col_list = columns or ["*"]
    table_list = f"{source_database}.{source_table}"

    # Determine connector type
    if source_db_type.lower() == "mysql":
        connector = "mysql-cdc"
    elif source_db_type.lower() == "postgresql":
        connector = "postgres-cdc"
    elif source_db_type.lower() in ("oracle",):
        connector = "oracle-cdc"
    else:
        connector = "mysql-cdc"

    config = {
        "flinkConfig": {
            "parallelism": 1,
            "checkpointInterval": checkpoint_interval,
            "checkpointMode": "EXACTLY_ONCE",
            "restartStrategy": "fixed-delay",
            "restartAttempts": 3,
            "restartDelay": "10s",
        },
        "source": {
            "connector": connector,
            "hostname": source_host,
            "port": source_port,
            "username": "${reader_username}",
            "password": "${reader_password}",
            "database-name": source_database,
            "table-list": table_list,
            "server-id": server_id,
            "startup-mode": startup_mode,
            "debezium": {
                "snapshot.mode": "initial" if startup_mode == "initial" else "schema_only",
                "decimal.handling.mode": "double",
            },
        },
        "sink": {
            "connector": "doris",
            "fenodes": "${doris_fe_url}",
            "table.identifier": target_table,
            "username": "${writer_username}",
            "password": "${writer_password}",
            "sink.properties": {
                "format": "json",
                "read_json_by_line": "true",
            },
        },
        "tableMapping": {
            "sourceTable": source_table,
            "targetTable": target_table,
            "columns": col_list,
            "enableDDL": True,
        },
    }
    return json.dumps(config, indent=2, ensure_ascii=False)


# ── Sandbox validation helpers ───────────────────────────────────


async def _sandbox_validate_drift_fixes(state, drifts: list) -> None:
    """Validate drift fix SQL in sandbox."""
    from wren_agent.tools.sandbox_sql import SandboxSQL
    sandbox = SandboxSQL()
    for drift in drifts:
        if drift.is_breaking and drift.auto_fix_sql:
            result = await sandbox.validate_ddl(drift.auto_fix_sql, drift.table_name)
            if not result["valid"]:
                state["warnings"] = state.get("warnings", []) + [
                    f"[Sandbox] Drift fix for '{drift.table_name}' INVALID: {result.get('error','?')[:120]}"
                ]
                drift.details += f"\n[SANDBOX FAIL] {result.get('suggestion','?')}"
                # Invalid DDL may still be reviewed as a breaking-change
                # acknowledgement, but it must never become deployable SQL.
                drift.auto_fix_sql = None


async def _sandbox_validate_datax(state, table_name: str, json_str: str) -> None:
    """Validate DataX JSON configuration in sandbox."""
    try:
        from wren_agent.tools.sandbox_sql import SandboxSQL
        sandbox = SandboxSQL()
        result = await sandbox.validate_datax_json(json_str)
        if not result["valid"]:
            state["warnings"] = state.get("warnings", []) + [
                f"[Sandbox] DataX config for '{table_name}' INVALID: {result.get('error','?')[:120]}"
            ]
            state["warnings"] = state.get("warnings", []) + [
                f"[Sandbox] Suggestion: {result.get('suggestion','?')}"
            ]
    except Exception:
        pass  # Sandbox validation is advisory
