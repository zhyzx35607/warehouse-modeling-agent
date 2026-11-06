"""Data Quality Monitoring Node — runs post-execution quality checks.

Checks:
  1. NULL ratio on key columns (ODS, DWD)
  2. Row count trends (compare current vs prior run)
  3. Data freshness (max timestamp vs expected recency)
  4. Cardinality validation (dimension key uniqueness)
  5. Aggregate consistency (DWS vs DWD totals)

Results feed into the construction report.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from wren_agent.state import AgentState


async def quality_monitor(state: AgentState) -> AgentState:
    """Run data quality checks on the completed DW.

    Tests:
    1. Row count validation (ODS vs source)
    2. NULL ratio on candidate columns
    3. Data freshness (date ranges)
    4. Cardinality checks
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    plan = state.get("design_plan")
    if plan is None:
        return state

    execution_results = state.get("execution_results", [])
    if not all(r.success for r in execution_results):
        state["warnings"] = state.get("warnings", []) + [
            "Skipping quality monitor — not all executions succeeded"
        ]
        return state

    checks: list[dict] = []

    try:
        from wren_agent.tools.doris_client import DorisClient
        from wren_agent.nodes.deployment import _warehouse_layers

        doris = DorisClient()
        landing = (
            dict(_warehouse_layers(state))
            if state.get("source_mode") == "warehouse" else {}
        )
        for key, value in (state.get("landing") or {}).items():
            name = str(key).lower()
            if value and not landing.get(name):
                landing[name] = str(value)

        # ── 1. ODS row counts ────────────────────────────────
        for ods in (plan.ods_designs or []):
            try:
                cnt = await doris.get_row_count(ods.ods_table)
                status = "pass" if cnt > 0 else "warn"
                checks.append({
                    "type": "row_count",
                    "layer": "ODS",
                    "table": ods.ods_table,
                    "value": cnt,
                    "status": status,
                    "detail": f"Row count: {cnt}" if cnt > 0 else "Table is empty — check source sync",
                })
            except Exception as e:
                checks.append({
                    "type": "row_count",
                    "layer": "ODS",
                    "table": ods.ods_table,
                    "status": "error",
                    "detail": str(e)[:200],
                })

        # ── 2. NULL ratio on DWD key columns ─────────────────
        from wren_agent.tools.column_profile import is_profile_candidate

        for dwd in (plan.dwd_designs or []):
            try:
                # Check first 5 non-PK columns for NULL ratio.
                # DATE/period columns are reported as WARN in 2b so an empty
                # ODS date copied into DWD cannot fail core_model publish
                # (task bb58438f).
                cols = []
                for field in (dwd.fields or [])[:5]:
                    name = field["name"] if isinstance(field, dict) else getattr(field, "name", "")
                    if not name or is_profile_candidate(field):
                        continue
                    cols.append(name)
                for col in cols:
                    nulls = await doris.query_value(
                        f"SELECT COUNT(*) FROM `{dwd.fact_table}` WHERE `{col}` IS NULL"
                    )
                    total = await doris.query_value(
                        f"SELECT COUNT(*) FROM `{dwd.fact_table}`"
                    )
                    if total and total > 0:
                        ratio = (nulls or 0) / total
                        status = "pass" if ratio < 0.01 else ("warn" if ratio < 0.1 else "fail")
                        checks.append({
                            "type": "null_ratio",
                            "layer": "DWD",
                            "table": dwd.fact_table,
                            "column": col,
                            "value": f"{ratio:.2%}",
                            "status": status,
                            "detail": f"{col} NULL ratio: {ratio:.2%} ({nulls}/{total})",
                        })
            except Exception:
                pass

        # ── 2b. Empty DATE/period columns on published DIM/DWD ──
        # Core modeling copies ODS columns as-is. A 100% NULL inventory_date
        # is a source-data fact, not a modeling defect — WARN so publish
        # still proceeds, and warehouse harness rejects using it as grain
        # (task bb58438f).
        from wren_agent.models.schema import FieldSchema, TableSchema
        from wren_agent.tools.column_profile import (
            attach_null_ratios, is_unusable_grain, populated_alternatives,
        )

        async def _warn_empty_dates(layer: str, raw_table: str, field_dicts: list) -> None:
            table = str(raw_table or "")
            if not table:
                return
            db, _, bare = table.partition(".")
            if "." not in table:
                bare = table
                db = landing.get(layer.lower(), "")
            cols = []
            for item in field_dicts or []:
                if isinstance(item, dict):
                    name = item.get("name") or ""
                    dtype = item.get("type") or item.get("data_type") or ""
                else:
                    name = getattr(item, "name", "") or ""
                    dtype = getattr(item, "data_type", "") or ""
                if name:
                    cols.append(FieldSchema(name=str(name), data_type=str(dtype)))
            if not cols:
                return
            schema = TableSchema(
                name=bare.strip("`"), schema_name=str(db or "").strip("`"),
                db_type="doris", fields=cols,
            )
            client = DorisClient(database=db) if db else doris
            try:
                await attach_null_ratios(schema, client.query_one)
            except Exception:
                return
            for field in schema.fields:
                if not is_unusable_grain(field.null_ratio):
                    continue
                alts = populated_alternatives(schema.fields, exclude=field.name)
                alt_txt = (
                    f" Populated alternatives: {', '.join(alts)}."
                    if alts else ""
                )
                checks.append({
                    "type": "null_ratio",
                    "layer": layer,
                    "table": table,
                    "column": field.name,
                    "value": f"{field.null_ratio:.0%}",
                    "status": "warn",
                    "detail": (
                        f"{field.name} is {field.null_ratio:.0%} NULL in the "
                        f"published {layer} table (copied from ODS). Warehouse "
                        f"DWS/ADS must not use it as a time grain.{alt_txt}"
                    ),
                })

        for dwd in (plan.dwd_designs or []):
            await _warn_empty_dates("DWD", dwd.fact_table, dwd.fields or [])
        for dim in (plan.dim_designs or []):
            await _warn_empty_dates("DIM", dim.dim_table, dim.fields or [])

        # ── 3. Data freshness ────────────────────────────────
        for ods in (plan.ods_designs or []):
            try:
                # Check _ods_load_time recency
                max_time = await doris.query_value(
                    f"SELECT MAX(_ods_load_time) FROM `{ods.ods_table}`"
                )
                if max_time:
                    # Parse datetime and check how recent
                    checks.append({
                        "type": "freshness",
                        "layer": "ODS",
                        "table": ods.ods_table,
                        "value": str(max_time)[:19],
                        "status": "pass",
                        "detail": f"Latest load: {str(max_time)[:19]}",
                    })
            except Exception:
                pass

        # ── 4. Cardinality (DIM key uniqueness) ──────────────
        for dim in (plan.dim_designs or []):
            if dim.surrogate_key and dim.natural_key:
                try:
                    total_keys = await doris.query_value(
                        f"SELECT COUNT(*) FROM `{dim.dim_table}`"
                    )
                    distinct_keys = await doris.query_value(
                        f"SELECT COUNT(DISTINCT `{dim.surrogate_key}`) FROM `{dim.dim_table}`"
                    )
                    if total_keys and total_keys > 0:
                        match = total_keys == distinct_keys
                        checks.append({
                            "type": "cardinality",
                            "layer": "DIM",
                            "table": dim.dim_table,
                            "value": f"{distinct_keys}/{total_keys}",
                            "status": "pass" if match else "warn",
                            "detail": f"Surrogate key uniqueness: {distinct_keys}/{total_keys}",
                        })
                except Exception:
                    pass

        # ── 4.5 DWS/ADS row counts ───────────────────────────
        # Catches "pipeline succeeded but wrote nothing" — an empty target
        # table after successful execution is a failure, not a pass.
        for layer_name, table in (
            [("DWS", d.summary_table) for d in (plan.dws_designs or [])]
            + [("ADS", a.ads_table) for a in (plan.ads_designs or [])]
        ):
            try:
                # Design targets may carry the layer db prefix (e.g.
                # `dws_ecommerce_orders_sh.dws_...`) while the client already
                # scopes to the landing db — counting the qualified name there
                # double-qualifies and errors (task 1e9b226c: 2 quality checks
                # errored, failing a run whose ADS tables were populated).
                bare = table.split(".", 1)[-1].strip("`")
                layer_client = DorisClient(
                    database=landing.get(layer_name.lower()) or None
                )
                cnt = await layer_client.get_row_count(bare)
                checks.append({
                    "type": "row_count",
                    "layer": layer_name,
                    "table": table,
                    "value": cnt,
                    "status": "pass" if cnt > 0 else "fail",
                    "detail": f"Row count: {cnt}" if cnt > 0
                              else "Target table is EMPTY after successful execution — pipeline wrote nothing",
                })
            except Exception as e:
                checks.append({
                    "type": "row_count",
                    "layer": layer_name,
                    "table": table,
                    "status": "error",
                    "detail": str(e)[:200],
                })

        # ── 5. Aggregate consistency (DWS sums vs DWD) ───────
        for dws in (plan.dws_designs or []):
            for measure in (dws.measures or [])[:2]:
                try:
                    m_name = measure.get("name", "") if isinstance(measure, dict) else str(measure)
                    if m_name:
                        detail_val = await doris.query_value(
                            f"SELECT SUM({m_name}) FROM `{dws.source_dwd_table}`"
                        )
                        summ_val = await doris.query_value(
                            f"SELECT SUM({m_name}) FROM `{dws.summary_table}`"
                        )
                        if detail_val is not None and summ_val is not None:
                            diff = abs(float(detail_val or 0) - float(summ_val or 0))
                            tolerance = float(detail_val) * 0.01 if detail_val else 1
                            checks.append({
                                "type": "aggregation",
                                "layer": "DWS",
                                "table": dws.summary_table,
                                "measure": m_name,
                                "value": f"diff={diff:.2f}",
                                "status": "pass" if diff < tolerance else "fail",
                                "detail": f"DWD={detail_val:.2f} vs DWS={summ_val:.2f} (diff={diff:.2f})",
                            })
                except Exception:
                    pass

    except Exception as e:
        state["warnings"] = state.get("warnings", []) + [
            f"Quality monitor execution error: {e}"
        ]

    # ── Summarize ───────────────────────────────────────────
    passed = sum(1 for c in checks if c.get("status") == "pass")
    warned = sum(1 for c in checks if c.get("status") == "warn")
    failed = sum(1 for c in checks if c.get("status") == "fail")
    errored = sum(1 for c in checks if c.get("status") == "error")

    state["quality_checks"] = {
        "total": len(checks),
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "errored": errored,
        "checks": checks,
        "run_at": now.isoformat(),
    }

    # Own the terminal status here — route_after_quality_monitor also sets it,
    # but state mutations in route functions are discarded by LangGraph, which
    # left successful tasks stuck at "designing" in PG (task d580a14f).
    from wren_agent.models.task import TaskStatus
    if state.get("status") != TaskStatus.FAILED:
        state["status"] = (
            TaskStatus.FAILED if (failed > 0 or errored > 0)
            else TaskStatus.COMPLETED
        )

    msg = (
        f"Quality: {passed} pass, {warned} warn, {failed} fail, "
        f"{errored} error ({len(checks)} checks)"
    )
    if failed > 0 or errored > 0:
        state["errors"] = state.get("errors", []) + [msg]
    elif warned > 0:
        state["warnings"] = state.get("warnings", []) + [msg]
    else:
        state["warnings"] = state.get("warnings", []) + [msg]

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "quality_monitor",
        "action": "completed",
        "checks": len(checks),
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "errored": errored,
        "timestamp": now.isoformat(),
    }]

    if (state.get("status") == TaskStatus.COMPLETED
            and state.get("source_mode", "business_db") == "business_db"
            and state.get("business_task_type", "full") in {"full", "core_model"}):
        try:
            from wren_agent.tools.core_handoff import finalize_core_handoff
            handoff = await finalize_core_handoff(state)
            state["audit_log"] = state.get("audit_log", []) + [{
                "phase": "quality_monitor", "action": "published_core_assets",
                "details": handoff, "timestamp": now.isoformat(),
            }]
        except Exception as exc:
            state["status"] = TaskStatus.FAILED
            state["errors"] = state.get("errors", []) + [
                f"Core asset handoff failed: {str(exc)[:300]}"
            ]

    return state
