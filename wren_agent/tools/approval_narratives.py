"""Background approval narratives — LLM interpretation blocks for the
design_plan approval detail page (wayfinder: design_plan 审批信息细化).

Three narrative blocks are generated in parallel when the task pauses at
human_approval, plus a computed `risk` block (always ready, no LLM):

- goal_interpretation  -> {"business": str, "technical": str}
- metric_interpretation -> {"interpretations": [{"metric_name", "interpretation"}]}
- expected_impact      -> {"narrative": str, "quantified": {...}}

Each LLM block retries twice with backoff, then degrades to status=failed
(the detail page falls back to pure structured rendering — approval flow
is never blocked). Auto mode generates the same way (T-01 decision).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from wren_agent.tools.llm_client import get_llm_client
from wren_agent.tools.pg_store import (
    load_approval_narratives,
    upsert_approval_narrative,
)

logger = logging.getLogger(__name__)

BLOCKS = ("goal_interpretation", "metric_interpretation", "expected_impact")

MAX_ATTEMPTS = 3  # 1 initial + 2 retries
RETRY_BACKOFF = (1.0, 3.0)
LLM_TIMEOUT = 180.0
MAX_METRICS_IN_PROMPT = 60


# ── Snapshot ─────────────────────────────────────────────────────


def build_narrative_snapshot(state: dict) -> dict:
    """Serialize everything the three prompts need, without holding state refs.

    Called synchronously in human_approval before the background task fires —
    the returned dict is the snapshot; later state mutations cannot affect it.
    """
    plan = state.get("design_plan")
    plan_dict = plan.model_dump(mode="json") if plan is not None else {}

    # Per-layer summary: table name / source / sync / field count / row estimate
    layer_summaries: list[dict] = []
    layers = (
        ("ODS", plan_dict.get("ods_designs", []), "ods_table", "source_table"),
        ("DIM", plan_dict.get("dim_designs", []), "dim_table", "source"),
        ("DWD", plan_dict.get("dwd_designs", []), "fact_table", "source_ods_tables"),
        ("DWS", plan_dict.get("dws_designs", []), "summary_table", "source_tables"),
        ("ADS", plan_dict.get("ads_designs", []), "ads_table", "source_dws_tables"),
    )
    for layer, designs, table_key, source_key in layers:
        tables = []
        for d in designs:
            tables.append({
                "table": d.get(table_key, ""),
                "source": d.get(source_key, ""),
                "fields": len(d.get("fields", [])),
                "sync": d.get("sync_method", "") if layer == "ODS" else "",
            })
        if tables:
            layer_summaries.append({"layer": layer, "tables": tables})

    # Metrics: DWS measures + ADS metrics, deduped by name
    metrics: list[dict] = []
    for d in plan_dict.get("dws_designs", []):
        for m in d.get("measures", []):
            metrics.append({
                "name": m.get("name", ""), "layer": "DWS",
                "expression": m.get("expression", ""), "grain": m.get("grain", "") or "、".join(d.get("grain", [])),
                "source": d.get("summary_table", ""),
            })
    for d in plan_dict.get("ads_designs", []):
        for m in d.get("metrics", []):
            metrics.append({
                "name": m.get("name", ""), "layer": "ADS",
                "expression": m.get("expression", ""), "grain": "、".join(d.get("grain", [])),
                "source": d.get("ads_table", ""),
                "registry_id": m.get("metric_registry_id"),
            })
    seen = set()
    deduped = []
    for m in metrics:
        key = m["name"]
        if key and key not in seen:
            seen.add(key)
            deduped.append(m)

    # Validation + risk markers
    validations = []
    for v in state.get("validation_results", []):
        if v.passed is False:
            validations.append({
                "level": str(v.level.value if hasattr(v.level, "value") else v.level),
                "layer": v.layer,
                "checks": v.checks[:5],
                "failures": v.failures[:5],
            })

    # Source-table row estimates from explorations (quantified impact note)
    source_rows = 0
    for exp in state.get("explorations", {}).values():
        for t in getattr(exp, "tables", []):
            source_rows += getattr(t, "row_count_estimate", 0) or 0

    return {
        "task_id": state.get("task_id", ""),
        "modeling_goal": state.get("modeling_goal", ""),
        "plan": plan_dict,
        "layer_summaries": layer_summaries,
        "metrics": deduped,
        "sql_artifact_count": len(plan_dict.get("sql_artifacts", [])),
        "validation_failures": validations,
        "all_validations_passed": state.get("all_validations_passed", False),
        "design_revision_count": state.get("design_revision_count", 0),
        "insufficient_gaps": state.get("insufficient_gaps", []),
        "schema_drift_count": len(state.get("schema_drifts", [])),
        "source_rows_estimate": source_rows,
    }


def build_risk_content(snapshot: dict) -> dict:
    """Computed risk block — validation failures + known-risk markers."""
    risks = []
    if not snapshot.get("all_validations_passed"):
        risks.append(
            f"校验 {len(snapshot.get('validation_failures', []))} 项未通过"
            f"（已修订 {snapshot.get('design_revision_count', 0)} 轮后升级人工）"
        )
    gaps = snapshot.get("insufficient_gaps", [])
    if gaps:
        risks.append(f"数仓不足以支撑缺口 {len(gaps)} 项（部分支撑继续）")
    if snapshot.get("schema_drift_count", 0):
        risks.append(f"Schema drift {snapshot.get('schema_drift_count', 0)} 项待处理")
    return {
        "validation_failures": snapshot.get("validation_failures", []),
        "revision_count": snapshot.get("design_revision_count", 0),
        "insufficient_gap_count": len(gaps),
        "schema_drift_count": snapshot.get("schema_drift_count", 0),
        "risks": risks,
    }


# ── Fire-and-forget entry (called from human_approval) ───────────


async def fire_approval_narratives(state: dict, approval_id: str) -> None:
    """Persist the computed risk block and launch the background LLM task.

    Must be awaited before the task pauses so the risk block is available
    instantly; the LLM blocks run detached and write when ready.
    """
    task_id = state.get("task_id", "")
    snapshot = build_narrative_snapshot(state)
    try:
        await upsert_approval_narrative(
            task_id, approval_id, "risk", "ready",
            content=build_risk_content(snapshot),
        )
    except Exception as e:
        logger.warning(f"[{task_id[:8]}] risk block persist failed: {e}")

    asyncio.create_task(generate_approval_narratives(task_id, approval_id, snapshot))


async def generate_approval_narratives(
    task_id: str, approval_id: str, snapshot: dict
) -> None:
    """Background task: generate the three LLM blocks in parallel.

    Each block is isolated — one failure degrades only that block.
    Runs detached from the graph; a process restart simply leaves the
    unfinished blocks pending (detail page degrades to structured).
    """
    try:
        await asyncio.gather(
            _generate_block(task_id, approval_id, "goal_interpretation", snapshot),
            _generate_block(task_id, approval_id, "metric_interpretation", snapshot),
            _generate_block(task_id, approval_id, "expected_impact", snapshot),
        )
    except Exception as e:  # gather-level safety net
        logger.error(f"[{task_id[:8]}] narrative generation crashed: {e}")


async def _generate_block(
    task_id: str, approval_id: str, block: str, snapshot: dict
) -> None:
    """Generate one block with 2 retries; persist ready or failed."""
    attempts = 0
    last_error = ""
    for delay in (*RETRY_BACKOFF, None):
        attempts += 1
        try:
            content = await _call_block(block, snapshot)
            await upsert_approval_narrative(
                task_id, approval_id, block, "ready",
                content=content, attempts=attempts,
            )
            logger.info(f"[{task_id[:8]}] narrative block {block} ready")
            return
        except Exception as e:
            last_error = str(e)[:500]
            logger.warning(f"[{task_id[:8]}] narrative block {block} attempt {attempts} failed: {e}")
            if delay is not None:
                await asyncio.sleep(delay)
    await upsert_approval_narrative(
        task_id, approval_id, block, "failed",
        error=last_error, attempts=attempts,
    )


async def _call_block(block: str, snapshot: dict) -> dict:
    """One LLM call for a block; returns parsed JSON content."""
    if block == "goal_interpretation":
        system, user = _goal_prompts(snapshot)
    elif block == "metric_interpretation":
        system, user = _metric_prompts(snapshot)
    else:
        system, user = _impact_prompts(snapshot)

    client = get_llm_client()
    raw = await asyncio.wait_for(
        client.generate_json(
            system_prompt=system, user_message=user, token_kind="report",
        ),
        timeout=LLM_TIMEOUT,
    )
    content = json.loads(raw)
    if not isinstance(content, dict):
        raise ValueError(f"block {block}: expected JSON object, got {type(content).__name__}")
    return content


# ── Prompts ──────────────────────────────────────────────────────


def _layer_summary_text(snapshot: dict) -> str:
    lines = []
    for ls in snapshot.get("layer_summaries", []):
        tables = ", ".join(
            f"{t['table']}({t.get('fields', 0)} 字段"
            + (f", 源 {t['source']}" if t.get("source") else "")
            + (f", {t['sync']}" if t.get("sync") else "")
            + ")"
            for t in ls["tables"]
        )
        lines.append(f"{ls['layer']}: {tables}")
    return "\n".join(lines) if lines else "（无设计）"


def _metric_list_text(snapshot: dict) -> str:
    metrics = snapshot.get("metrics", [])[:MAX_METRICS_IN_PROMPT]
    lines = []
    for m in metrics:
        reg = "已注册 registry" if m.get("registry_id") else "未注册"
        lines.append(
            f"- {m['name']}（{m['layer']}，粒度 {m.get('grain') or '—'}，"
            f"来源 {m.get('source') or '—'}，{reg}）：{m.get('expression') or '—'}"
        )
    return "\n".join(lines) if lines else "（无指标）"


def _goal_prompts(snapshot: dict) -> tuple[str, str]:
    system = (
        "你是资深数仓建模评审助手。根据建模目标与设计方案，输出「目标解读」，"
        "分为业务解读与技术解读两段。\n"
        "业务解读：面向业务/管理审批人，不用技术术语，说明解决了什么业务问题、"
        "对业务有何价值。\n"
        "技术解读：面向数据团队，概述分层设计动机、关键取舍与依赖关系。\n"
        "输出格式：JSON 对象 {\"business\": string, \"technical\": string}。"
        "每段 120-200 字，直接输出 JSON，无 markdown。"
    )
    if snapshot.get("all_validations_passed"):
        validation_note = "全部通过"
    else:
        validation_note = (
            f"{len(snapshot.get('validation_failures', []))} 项未通过"
            f"（修订 {snapshot.get('design_revision_count', 0)} 轮）"
        )
    user = (
        f"建模目标：{snapshot.get('modeling_goal', '')}\n\n"
        f"方案设计：\n{_layer_summary_text(snapshot)}\n\n"
        f"SQL 产物 {snapshot.get('sql_artifact_count', 0)} 条；"
        f"校验 {validation_note}。"
    )
    return system, user


def _metric_prompts(snapshot: dict) -> tuple[str, str]:
    system = (
        "你是资深数仓指标口径分析师。对清单中的每个指标输出一句业务口径解读："
        "为什么是这个口径、反映什么业务问题、口径中有什么需要注意的取舍。\n"
        "输出格式：JSON 对象 {\"interpretations\": [{\"metric_name\": string, \"interpretation\": string}]}。"
        "必须覆盖清单中全部指标；每句 40-80 字；直接输出 JSON，无 markdown。"
    )
    user = (
        f"指标清单（共 {len(snapshot.get('metrics', []))} 个）：\n"
        f"{_metric_list_text(snapshot)}"
    )
    return system, user


def _impact_prompts(snapshot: dict) -> tuple[str, str]:
    ads_targets = []
    for d in snapshot.get("plan", {}).get("ads_designs", []):
        t = d.get("target_audience", "")
        if t:
            ads_targets.append(f"{d.get('ads_table', '')} → {t}")
    metrics = snapshot.get("metrics", [])
    derived = [m for m in metrics if m.get("layer") == "ADS"]
    quantified = {
        "new_tables": sum(len(ls["tables"]) for ls in snapshot.get("layer_summaries", [])),
        "new_metrics": len(metrics),
        "derived_metrics": len(derived),
        "source_rows_estimate": snapshot.get("source_rows_estimate", 0),
        "target_applications": ads_targets,
    }
    system = (
        "你是数仓建设价值评估助手。根据设计方案输出「预计效果」：业务价值叙事 + 量化附注。\n"
        "叙事：面向审批人说明方案落地后业务能获得什么（新报表/看板、替代手工流程、"
        "口径统一、指标复用等），80-150 字。\n"
        "量化附注：从给定数据如实填入，不得编造。\n"
        "输出格式：JSON 对象 {\"narrative\": string, \"quantified\": {"
        "\"new_tables\": number, \"new_metrics\": number, \"derived_metrics\": number, "
        "\"source_rows_estimate\": number, \"target_applications\": [string]}}。"
        "直接输出 JSON，无 markdown。"
    )
    user = (
        f"方案设计：\n{_layer_summary_text(snapshot)}\n\n"
        f"指标清单（{len(metrics)} 个）：{', '.join(m['name'] for m in metrics[:20])}\n"
        f"目标应用：{'; '.join(ads_targets) if ads_targets else '（未指定）'}\n"
        f"源数据行量级估算：{snapshot.get('source_rows_estimate', 0):,}"
    )
    return system, user
