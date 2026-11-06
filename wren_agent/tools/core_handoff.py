"""Handoff validated DIM/DWD outputs to the existing warehouse entry."""

from __future__ import annotations

import re
from typing import Any


def validate_core_landing(landing: dict[str, str]) -> dict[str, str]:
    required = ("ods", "dim", "dwd")
    missing = [layer for layer in required if not str(landing.get(layer, "")).strip()]
    if missing:
        raise ValueError(f"missing landing databases: {', '.join(missing)}")
    out = {layer: str(landing[layer]).strip() for layer in required}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in out.values()):
        raise ValueError("landing database names must be valid Doris identifiers")
    return out


async def ensure_core_databases(state: dict[str, Any]) -> None:
    """Create configured databases through the explicitly registered Doris target."""
    if not state.get("create_missing_databases"):
        return
    raw_landing = state.get("landing") or {}
    required = (("ods",) if state.get("business_task_type") == "ods_migration"
                else ("ods", "dim", "dwd"))
    landing = {layer: str(raw_landing.get(layer, "")).strip() for layer in required}
    missing = [layer for layer, value in landing.items() if not value]
    if missing:
        raise ValueError(f"missing landing databases: {', '.join(missing)}")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
           for value in landing.values()):
        raise ValueError("landing database names must be valid Doris identifiers")
    from wren_agent.tools.doris_client import DorisClient
    from wren_agent.tools.pg_store import get_source_db_credentials
    target = await get_source_db_credentials(state.get("warehouse_source_id", ""))
    if not target or str(target.get("db_type", "")).lower() != "doris":
        raise ValueError("warehouse_source_id must resolve to Doris credentials")
    # Open the session on the task's ODS landing — never on
    # source_db_registry.database_name. That field is the warehouse
    # registration's default catalog and has been observed missing on the
    # cluster (task ac119218: `dwd_ecommerce_orders` → pymysql 1049 before
    # CREATE DATABASE ran). If the landing ODS is itself new, fall back to
    # no catalog so CREATE DATABASE can still run.
    session_db = landing.get("ods") or ""
    client = DorisClient(host=target["host"], port=target["port"], user=target["user"],
                         password=target["password"], database=session_db)
    pending = list(dict.fromkeys(landing.values()))
    retried_without_catalog = False
    while pending:
        database = pending[0]
        try:
            await client.execute_ddl(f"CREATE DATABASE IF NOT EXISTS `{database}`")
            pending.pop(0)
        except Exception as exc:
            if (session_db and not retried_without_catalog
                    and _is_unknown_database(exc)):
                client = DorisClient(
                    host=target["host"], port=target["port"], user=target["user"],
                    password=target["password"], database="",
                )
                retried_without_catalog = True
                continue
            raise


def _is_unknown_database(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "unknown database" in text or "1049" in text


async def publish_core_assets(state: dict[str, Any], core_model_version: int) -> dict[str, Any]:
    """Merge DIM/DWD databases into the existing warehouse registration."""
    from wren_agent.tools.pg_store import (
        get_pg_pool, merge_source_db_layers, register_metric, update_source_db_status,
    )

    if state.get("source_mode", "business_db") != "business_db":
        return {"published": False, "reason": "not_business_mode"}
    quality = state.get("quality_checks") or {}
    if quality.get("failed", 0) or quality.get("errored", 0):
        return {"published": False, "reason": "quality_gate_failed"}
    landing = validate_core_landing(state.get("landing") or {})
    source_id = state.get("warehouse_source_id", "")
    layers = await merge_source_db_layers(source_id, {"dim": landing["dim"], "dwd": landing["dwd"]})
    plan = state.get("design_plan")
    if plan is None:
        raise ValueError("core asset publication requires design_plan")

    # DWD measures are the atomic metrics consumed by warehouse-mode DWS design.
    for dwd in (plan.dwd_designs or []):
        source_table = f"{landing['dwd']}.{str(dwd.fact_table).split('.')[-1]}"
        for measure in (dwd.measures or []):
            if not isinstance(measure, dict) or not str(measure.get("name", "")).strip():
                continue
            await register_metric(
                metric_name=str(measure["name"]), metric_type="atomic",
                definition_sql=str(measure.get("expression") or measure.get("name")),
                source_table=source_table, owner_task_id=state["task_id"],
                grain=",".join(dwd.grain or []), tags=["core_model"],
            )

    assets: list[tuple] = []
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        for dim in (plan.dim_designs or []):
            table = str(dim.dim_table).split(".")[-1]
            registry_id = await conn.fetchval(
                "SELECT dim_id FROM dimension_registry WHERE owner_task_id=$1 "
                "AND dim_table=$2 AND active=TRUE ORDER BY version DESC LIMIT 1",
                state["task_id"], table,
            )
            assets.append(("dimension", "dim", landing["dim"], table, registry_id, None, {}))
        for dwd in (plan.dwd_designs or []):
            table = str(dwd.fact_table).split(".")[-1]
            dag = await conn.fetchrow(
                "SELECT workflow_code FROM dag_table_map WHERE table_name IN ($1, $2) LIMIT 1",
                table, f"{landing['dwd']}.{table}",
            )
            assets.append(("table", "dwd", landing["dwd"], table, None,
                           dag["workflow_code"] if dag else None, {"grain": dwd.grain}))
            rows = await conn.fetch(
                "SELECT metric_id, metric_name FROM metric_registry WHERE owner_task_id=$1 "
                "AND source_table=$2 AND active=TRUE", state["task_id"], f"{landing['dwd']}.{table}",
            )
            for row in rows:
                assets.append(("metric", "dwd", landing["dwd"], table,
                               row["metric_id"], None, {"metric_name": row["metric_name"]}))
        for kind, layer, database, table, registry_id, workflow_code, metadata in assets:
            await conn.execute(
                "INSERT INTO core_asset_lineage "
                "(task_id, core_model_version, warehouse_source_id, asset_kind, layer, "
                "database_name, table_name, registry_id, workflow_code, metadata) "
                "VALUES ($1,$2,$3::uuid,$4,$5,$6,$7,$8,$9,$10::jsonb) ON CONFLICT DO NOTHING",
                state["task_id"], core_model_version, source_id, kind, layer,
                database, table, registry_id, workflow_code, __import__("json").dumps(metadata),
            )

    # Refresh table counts from the target databases; the next warehouse task
    # still performs normal schema exploration for columns and relationships.
    from wren_agent.tools.doris_client import DorisClient
    from wren_agent.tools.pg_store import get_source_db_credentials
    target = await get_source_db_credentials(source_id)
    total = 0
    if target:
        for database in dict.fromkeys((landing["dim"], landing["dwd"])):
            client = DorisClient(host=target["host"], port=target["port"], user=target["user"],
                                 password=target["password"], database=database)
            total += len(await client.list_tables())
        await update_source_db_status(source_id, "active", total)
    return {
        "published": True,
        "warehouse_source_id": source_id,
        "core_model_version": core_model_version,
        "layers": layers,
        "assets": len(assets),
    }


async def finalize_core_handoff(state: dict[str, Any]) -> dict[str, Any]:
    """Persist a CoreModelPlan version, then publish it to warehouse mode."""
    from datetime import datetime, timezone
    from wren_agent.models.stage import ModelingStageRun, StageStatus
    from wren_agent.tools.pg_store import (
        create_stage_artifact, upsert_modeling_stage,
    )
    plan = state.get("design_plan")
    if plan is None:
        raise ValueError("missing design_plan")
    await upsert_modeling_stage(ModelingStageRun(
        task_id=state["task_id"], stage="core_model",
        thread_id=f"{state['task_id']}:core_model", status=StageStatus.SUCCEEDED,
        input_versions={}, quality_gate=state.get("quality_checks") or {},
        finished_at=datetime.now(timezone.utc),
    ))
    artifact = await create_stage_artifact(
        state["task_id"], "core_model", "core_model_plan",
        {"dim_designs": [d.model_dump(mode="json") for d in (plan.dim_designs or [])],
         "dwd_designs": [d.model_dump(mode="json") for d in (plan.dwd_designs or [])]},
    )
    return await publish_core_assets(state, int(artifact["version"]))
