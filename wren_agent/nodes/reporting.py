"""Reporting & KG export nodes — final output generation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from wren_agent.models.task import ClarificationStatus, TaskPhase, TaskStatus
from wren_agent.state import AgentState

logger = logging.getLogger(__name__)


def _get(obj, name: str, default=None):
    """Read a field from either a Pydantic model or persisted plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _list(obj, name: str) -> list:
    return list(_get(obj, name, []) or [])


def _md_cell(value) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ")


def _field_summary(table, limit: int = 5) -> str:
    fields = _list(table, "fields")
    labels = []
    for field in fields[:limit]:
        name = _get(field, "name", "")
        comment = _get(field, "comment", "")
        if name:
            labels.append(f"{name}（{comment}）" if comment else str(name))
    if len(fields) > limit:
        labels.append(f"等 {len(fields)} 个字段")
    return "、".join(labels) or "未获取字段说明"


def _plain_output_rows(plan) -> list[tuple[str, str, str, str]]:
    """Return layer/table/purpose/contents rows for the business report."""
    if not plan:
        return []
    rows: list[tuple[str, str, str, str]] = []
    for design in _list(plan, "ods_designs"):
        rows.append((
            "ODS 原始层", _get(design, "ods_table", ""),
            "保留源系统数据，便于追溯",
            f"来自 {_get(design, 'source_table', '未知源表')}",
        ))
    for design in _list(plan, "dim_designs"):
        rows.append((
            "DIM 维度层", _get(design, "dim_table", ""),
            _get(design, "dim_name", "统一业务分类和基础信息"),
            _field_summary(design),
        ))
    for design in _list(plan, "dwd_designs"):
        rows.append((
            "DWD 明细层", _get(design, "fact_table", ""),
            _get(design, "business_process", "整理可统计的业务明细"),
            _field_summary(design),
        ))
    for design in _list(plan, "dws_designs"):
        measures = [
            _get(item, "name", "") for item in _list(design, "measures")
            if _get(item, "name", "")
        ]
        rows.append((
            "DWS 汇总层", _get(design, "summary_table", ""),
            _get(design, "summary_name", "按固定粒度汇总业务数据"),
            "、".join(measures[:8]) or _field_summary(design),
        ))
    for design in _list(plan, "ads_designs"):
        metrics = [
            _get(item, "name", "") for item in _list(design, "metrics")
            if _get(item, "name", "")
        ]
        purpose = _get(design, "description", "") or _get(design, "application_name", "")
        # The programmatic fallback's description is an English template
        # dumping the whole modeling goal — useless as a purpose line.
        if purpose.startswith("Application metric table"):
            purpose = ""
        rows.append((
            "ADS 应用层", _get(design, "ads_table", ""),
            purpose or "面向报表/看板的指标结果表",
            "、".join(metrics[:8]) or _field_summary(design),
        ))
    return [row for row in rows if row[1]]


def build_plain_report(state: dict, now: datetime | None = None) -> str:
    """Build a deterministic report for non-technical business readers.

    It deliberately avoids raw SQL, rule IDs, and long validation messages.
    Persisted dicts and live AgentState models are both supported so historical
    tasks can receive the companion report on demand.
    """
    now = now or datetime.now(timezone.utc)
    goal = _get(state, "modeling_goal", "") or "未填写建模目标"
    plan = _get(state, "design_plan")
    explorations = _get(state, "explorations", {}) or {}
    validations = _list(state, "validation_results")
    executions = _list(state, "execution_results")
    errors = _list(state, "errors")
    warnings = _list(state, "warnings")
    quality = _get(state, "quality_checks", {}) or {}
    status = _get(state, "status", "")
    status_value = getattr(status, "value", str(status or ""))

    failed_validations = sum(1 for item in validations if _get(item, "passed") is False)
    validation_error_count = int(
        _get(state, "validation_error_count", failed_validations) or 0
    )
    pending_validations = sum(1 for item in validations if _get(item, "passed") is None)
    rows_by_table: dict[str, int] = {}
    for execution in executions:
        for table, count in (_get(execution, "rows_written", {}) or {}).items():
            rows_by_table[table] = rows_by_table.get(table, 0) + int(count or 0)
    total_rows = sum(rows_by_table.values())

    if (
        status_value == "failed"
        or errors
        or failed_validations
        or validation_error_count
        or quality.get("failed", 0)
        or quality.get("errored", 0)
    ):
        conclusion = "尚未达到可直接使用的标准，需要先处理报告中列出的问题。"
        conclusion_level = "需要处理"
    elif status_value == "completed" and total_rows > 0:
        conclusion = "建模和数据写入已完成，可进入业务验收和报表对数阶段。"
        conclusion_level = "已完成"
    elif status_value == "completed":
        conclusion = "建模流程已完成，但未观察到目标表写入行数，使用前请先确认数据是否落地。"
        conclusion_level = "待确认"
    else:
        conclusion = "任务尚未完全结束，本报告展示当前已完成的探索和设计结果。"
        conclusion_level = "进行中"

    sections = [
        "# 通俗版数据建模报告",
        "",
        f"**建模目标**：{goal}",
        f"**当前结论**：{conclusion_level}",
        "",
        f"> {conclusion}",
        "",
        "## 1. 这次想解决什么问题",
        "",
        goal,
        "",
        "## 2. 系统找到了哪些数据",
        "",
    ]

    exploration_rows = []
    all_explored_tables = []
    for source, exploration in explorations.items():
        tables = _list(exploration, "tables")
        all_explored_tables.extend((source, table) for table in tables)
        exploration_rows.append((source, len(tables)))

    # The report answers the modeling goal — the full exploration inventory
    # (hundreds of unrelated tables) is noise; one summary line is enough,
    # the relevant sources are listed below.
    if exploration_rows:
        sections.append(
            f"共检查 **{len(exploration_rows)}** 个数据范围、**{len(all_explored_tables)}** 张表；"
            "下面只列出与本次目标直接相关的源数据，其余从略。"
        )
    else:
        sections.append("当前没有可用的数据探索记录。")

    used_tables = {name.lower().split(".")[-1] for name in _collect_used_tables(plan)}
    relevant = [
        (source, table) for source, table in all_explored_tables
        if str(_get(table, "name", "")).lower() in used_tables
    ]
    sections.extend(["", "### 与建模目标直接相关的源数据", ""])
    if relevant:
        sections.extend([
            "| 数据表 | 所属范围 | 表含义 | 主要字段 |",
            "|---|---|---|---|",
        ])
        seen = set()
        for source, table in relevant:
            name = str(_get(table, "name", ""))
            # The same physical table can be explored under several layer
            # scopes (dwd/dwm) — show it once.
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            sections.append(
                f"| `{_md_cell(name)}` | {_md_cell(source)} | "
                f"{_md_cell(_get(table, 'comment', '') or '未提供表说明')} | "
                f"{_md_cell(_field_summary(table))} |"
            )
    elif all_explored_tables:
        sections.append("方案尚未明确标记最终使用的源表；可先参考上方各数据范围的代表性表。")
    else:
        sections.append("暂无相关数据表可展示。")

    sections.extend(["", "## 3. 系统准备生成什么结果", ""])
    output_rows = _plain_output_rows(plan)
    if output_rows:
        sections.extend([
            "| 数据层级 | 结果表 | 主要用途 | 包含的主要内容 |",
            "|---|---|---|---|",
        ])
        for layer, table, purpose, contents in output_rows:
            sections.append(
                f"| {_md_cell(layer)} | `{_md_cell(table)}` | {_md_cell(purpose)} | {_md_cell(contents)} |"
            )
    else:
        sections.append("尚未生成可展示的数据表方案。")

    passed_validations = sum(1 for item in validations if _get(item, "passed") is True)
    sections.extend([
        "",
        "## 4. 结果是否可以直接使用",
        "",
        f"- 校验情况：{passed_validations} 项通过，{failed_validations} 项未通过，{pending_validations} 项待确认。",
        f"- 数据写入：共观察到 {total_rows:,} 行数据写入目标表。",
        f"- 数据质量：{quality.get('passed', 0)} 项通过，{quality.get('failed', 0)} 项失败，{quality.get('errored', 0)} 项执行异常。",
        f"- 系统提示：{len(warnings)} 条需要关注的信息，{len(errors)} 条阻断性问题。",
        "",
        f"**结论：{conclusion}**",
        "",
        "## 5. 业务人员下一步建议",
        "",
    ])
    if failed_validations or validation_error_count or errors:
        sections.extend([
            "1. 先不要将当前结果用于正式报表或经营决策。",
            "2. 请数据开发人员根据“校验”页中的通俗说明修正表结构、指标口径或数据来源。",
            "3. 修正后重新运行，并请业务人员抽样核对关键指标。",
        ])
    else:
        sections.extend([
            "1. 抽取几个典型时间段和组织，与原系统或手工台账对数。",
            "2. 确认指标的业务口径、时间范围和统计维度符合预期。",
            "3. 验收通过后，再接入报表、看板或定时任务。",
        ])

    sections.extend([
        "",
        "---",
        f"报告生成时间：{now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "说明：本报告面向业务阅读，技术 SQL、完整校验规则和调度细节仍保留在原“建设报告”中。",
    ])
    return "\n".join(sections)


async def generate_report(state: AgentState) -> AgentState:
    """Generate the construction report.

    Summarizes the entire modeling task:
    - Task metadata (goal, duration, status)
    - Schema exploration summary
    - Knowledge collected (clarifications answered)
    - DW design summary (layers, tables, SQLs)
    - Validation results
    - Execution results
    - Data quality metrics
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.REPORT

    # A breaking-drift rejection arrives here directly from route_after_approval
    # (state mutations inside route functions are discarded by LangGraph, so
    # the report node owns the FAILED flag for this path).
    if state.get("status") != TaskStatus.FAILED and _has_rejected_breaking_drift(state):
        state["status"] = TaskStatus.FAILED
        msg = "Breaking schema drift was rejected; stopping before warehouse design"
        if msg not in state.get("errors", []):
            state["errors"] = state.get("errors", []) + [msg]

    # Tasks that failed BEFORE deployment get a plain-language failure
    # report instead of nothing — every fail-fast route in graph.py funnels
    # here so /report never 404s for a finished-but-failed task. Tasks that
    # failed after execution (e.g. data quality) keep the full report, which
    # already records the truthful outcome.
    _failed_early = state.get("status") == TaskStatus.FAILED and not (
        state.get("execution_results") or state.get("quality_checks")
    )
    if _failed_early:
        state["construction_report"] = build_failure_report(state)
        state["plain_report"] = build_plain_report(state, now=now)

        from wren_agent.tools.event_emitter import emit_phase
        emit_phase(state, "generate_report")
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "generate_report",
            "action": "failure_report",
            "timestamp": now.isoformat(),
        }]
        return state

    plan = state.get("design_plan")
    validations = state.get("validation_results", [])
    executions = state.get("execution_results", [])
    clarifications = state.get("clarifications", [])
    sensitive = state.get("sensitive_fields", [])
    drifts = state.get("schema_drifts", [])

    # Build report sections
    sections = []

    # Honest outcome: never let the header claim "成功" when quality checks
    # failed or errors were recorded (task 6521caff reported success over
    # empty tables).
    quality = state.get("quality_checks", {}) or {}
    quality_failed = quality.get("failed", 0)
    if state.get("errors"):
        outcome = "完成（存在错误，详见错误节）"
    elif quality_failed:
        outcome = f"完成（{quality_failed} 项质量检查未通过，目标表可能无数据）"
    elif state.get("status") == TaskStatus.COMPLETED:
        outcome = "成功"
    else:
        outcome = str(getattr(state.get("status"), "value", state.get("status")))

    # Header
    sections.append(f"# 数仓建模建设报告\n")
    sections.append(f"**Task ID**: {state['task_id']}")
    sections.append(f"**建模目标**: {state['modeling_goal']}")
    sections.append(f"**状态**: {outcome}")
    sections.append(f"**耗时**: {_calculate_duration(state.get('created_at'), now)}")
    sections.append("")

    # Artifact overview — the first thing a reader wants to know: which
    # target tables were produced and how many rows actually landed in each.
    rows_by_table: dict[str, int] = {}
    for e in executions:
        for tbl, n in (e.rows_written or {}).items():
            rows_by_table[tbl] = rows_by_table.get(tbl, 0) + n

    if plan:
        section = [
            "## 产物一览", "",
            "| 层 | 目标表 | 实际写入行数 |",
            "|---|---|---|",
        ]

        def _artifact_row(layer: str, table: str | None):
            if not table:
                return
            rows = rows_by_table.get(table)
            rows_txt = f"{rows:,}" if rows is not None else "—（未观察到行数）"
            section.append(f"| {layer} | `{table}` | {rows_txt} |")

        for ods in (plan.ods_designs or []):
            _artifact_row("ODS", ods.ods_table)
        for dim in (plan.dim_designs or []):
            _artifact_row("DIM", dim.dim_table)
        for dwd in (plan.dwd_designs or []):
            _artifact_row("DWD", dwd.fact_table)
        for dws in (plan.dws_designs or []):
            _artifact_row("DWS", dws.summary_table)
        for ads in (plan.ads_designs or []):
            _artifact_row("ADS", ads.ads_table)
        section.append("")
        sections.append("\n".join(section) + "\n")

    # Schema exploration — summarized, not dumped: one line per explored
    # group plus the tables the design actually consumed (task 6521caff's
    # report listed 70+ raw table lines, repeated per mirrored database).
    explorations = state.get("explorations", {})
    used_tables = _collect_used_tables(plan)
    section = ["## 1. 数据探索"]
    if explorations:
        total_tables = 0
        for db_id, expl in explorations.items():
            total_tables += len(expl.tables)
            section.append(f"- `{db_id}`: 探索 {len(expl.tables)} 张表")
        section.append(f"- 共探索 {total_tables} 张表（含跨库重名），设计实际引用 {len(used_tables)} 张")
        if used_tables:
            section.append(f"- 设计引用的表: {', '.join(f'`{t}`' for t in sorted(used_tables))}")
    else:
        section.append("未执行数据探索。")
    sections.append("\n".join(section) + "\n")

    # Sensitive fields — the report answers the modeling goal, so only
    # fields on tables the design actually uses are shown; fields from
    # unrelated explored tables are exploration noise (task d580a14f listed
    # 29 address/name fields from tables the design never touched).
    used_bare = {t.lower().split(".")[-1] for t in used_tables}
    relevant_sf = [
        sf for sf in sensitive
        if (sf.table_name or "").lower().split(".")[-1] in used_bare
    ]
    section = ["## 2. 敏感字段"]
    if relevant_sf:
        for sf in relevant_sf:
            mask = sf.mask_strategy or "未确认"
            section.append(f"- `{sf.table_name}.{sf.field_name}` → 类型: {sf.sensor_type.value}, 策略: {mask}")
        skipped = len(sensitive) - len(relevant_sf)
        if skipped > 0:
            section.append(f"- 另有 {skipped} 个敏感字段来自未被本方案引用的表，与本次目标无关，从略。")
    elif sensitive:
        section.append(f"探索共发现 {len(sensitive)} 个敏感字段，均不在本方案引用的表中，与本次目标无关，从略。")
    else:
        section.append("未检测到敏感字段。")
    sections.append("\n".join(section) + "\n")

    # Knowledge collected
    section = ["## 3. 业务知识"]
    answered = [
        c for c in clarifications
        if getattr(c, "status", None) == ClarificationStatus.ANSWERED
    ]
    pending = [
        c for c in clarifications
        if getattr(c, "status", None) == ClarificationStatus.PENDING
    ]
    section.append(f"- 已回答: {len(answered)} 条")
    section.append(f"- 未回答: {len(pending)} 条")
    sections.append("\n".join(section) + "\n")

    # Design summary
    if plan:
        section = ["## 4. 数仓方案"]
        section.append(f"- ODS 表: {len(plan.ods_designs or [])}")
        section.append(f"- DIM 表: {len(plan.dim_designs or [])}")
        section.append(f"- DWD 表: {len(plan.dwd_designs or [])}")
        section.append(f"- DWS 表: {len(plan.dws_designs or [])}")
        section.append(f"- ADS 表: {len(plan.ads_designs or [])}")
        section.append(f"- SQL 产物: {len(plan.sql_artifacts or [])} 条")
        section.append(f"")
        # Table details
        for ods in (plan.ods_designs or []):
            section.append(f"  ODS: `{ods.ods_table}` ← `{ods.source_table}` (sync: {ods.sync_method})")
        for dim in (plan.dim_designs or []):
            section.append(f"  DIM: `{dim.dim_table}` (key: {dim.natural_key})")
        for dwd in (plan.dwd_designs or []):
            section.append(f"  DWD: `{dwd.fact_table}` (grain: {dwd.grain})")
        for dws in (plan.dws_designs or []):
            section.append(f"  DWS: `{dws.summary_table}` (grain: {dws.grain})")
        sections.append("\n".join(section) + "\n")

    # Validation results
    section = ["## 5. 数据校验"]
    if validations:
        passed = [v for v in validations if v.passed]
        failed = [v for v in validations if v.passed is False]
        section.append(f"- 结构校验: {'通过' if all(v.passed for v in validations if v.level.value == 'structure') else '失败'}")
        section.append(f"- 一致性校验: {'通过' if all(v.passed for v in validations if v.level.value == 'consistency') else '失败'}")
        section.append(f"- 语义校验: 已生成 {sum(len(v.semantic_assertions) for v in validations if v.level.value == 'semantic')} 条断言")
    sections.append("\n".join(section) + "\n")

    # ── Data Quality Checks ──────────────────────────────────
    if quality:
        section = ["## 6. 数据质量检查"]
        section.append(f"- 总检查项: {quality.get('total', 0)}")
        section.append(f"- 通过: {quality.get('passed', 0)}")
        section.append(f"- 警告: {quality.get('warned', 0)}")
        section.append(f"- 失败: {quality.get('failed', 0)}")
        section.append(f"- 执行错误: {quality.get('errored', 0)}")
        section.append(f"- 检查时间: {quality.get('run_at', 'N/A')}")
        section.append("")

        # Detailed check results by type
        checks = quality.get("checks", [])
        if checks:
            by_type: dict[str, list] = {}
            for c in checks:
                ct = c.get("type", "other")
                by_type.setdefault(ct, []).append(c)

            type_labels = {
                "row_count": "行数验证",
                "null_ratio": "NULL 比率",
                "freshness": "数据新鲜度",
                "cardinality": "代理键唯一性",
                "aggregation": "聚合一致性",
            }
            for ct, items in by_type.items():
                label = type_labels.get(ct, ct)
                section.append(f"### {label} ({len(items)} 项)")
                for item in items[:10]:
                    status_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "error": "💥"}.get(item.get("status", ""), "❓")
                    section.append(f"  {status_icon} {item.get('table', '?')}: {item.get('detail', '')}")
                section.append("")
        sections.append("\n".join(section) + "\n")

    # Execution results
    section = ["## 7. 执行结果"]
    if executions:
        success_count = sum(1 for e in executions if e.success)
        section.append(f"- 成功: {success_count}/{len(executions)}")
        total_rows = sum(sum(e.rows_written.values()) for e in executions)
        section.append(f"- 总写入行数: {total_rows}")
        section.append("")
        for e in executions:
            icon = "✅" if e.success else "❌"
            rows_txt = (
                ", ".join(f"`{t}` {n:,} 行" for t, n in (e.rows_written or {}).items())
                or "无行数记录"
            )
            section.append(f"  {icon} `{e.task_instance_id}` — {rows_txt}")
    sections.append("\n".join(section) + "\n")

    # Schema drifts
    if drifts:
        section = ["## 8. Schema 漂移"]
        for d in drifts:
            section.append(f"- `{d.table_name}`: {d.drift_type.value} — {d.details}")
        sections.append("\n".join(section) + "\n")

    # Warnings & errors — cap the dump: dozens of process-level warnings
    # (sandbox INVALID, DAG dependency notes) bury the goal-relevant results.
    warnings = state.get("warnings", [])
    errors = state.get("errors", [])
    if warnings:
        section = ["## 9. 告警"]
        for w in warnings[:10]:
            section.append(f"- {w}")
        if len(warnings) > 10:
            section.append(f"- …其余 {len(warnings) - 10} 条过程性告警从略。")
        sections.append("\n".join(section) + "\n")
    if errors:
        section = ["## 10. 错误"]
        for e in errors:
            section.append(f"- {e}")
        sections.append("\n".join(section) + "\n")

    # Evidence-first narrative summary. The model may polish these facts but
    # must not infer data correctness from a successful scheduler status.
    status = state.get("status", TaskStatus.FAILED)
    status_value = getattr(status, "value", str(status))
    total_rows = sum(sum(e.rows_written.values()) for e in executions)
    failed_validations = sum(1 for v in validations if v.passed is False)
    validation_error_count = int(
        state.get("validation_error_count", failed_validations) or 0
    )
    pending_validations = sum(1 for v in validations if v.passed is None)
    quality = state.get("quality_checks", {}) or {}
    plan_artifacts = list(plan.sql_artifacts or []) if plan else []
    dml_count = sum(1 for a in plan_artifacts if a.sql_type.upper().startswith("DML_"))
    deterministic_summary = (
        f"- 任务状态: {status_value}\n"
        f"- 物化: {dml_count} 条 DML 装载语句，目标表实际写入 {total_rows} 行\n"
        f"- 校验: {validation_error_count} 条具体错误，"
        f"{failed_validations} 项失败，"
        f"{pending_validations} 项语义校验待复核\n"
        f"- 质量: {quality.get('failed', 0)} 项失败，{quality.get('errored', 0)} 项异常"
    )

    try:
        from wren_agent.tools.llm_client import get_llm_client
        llm = get_llm_client()
        summary_prompt = f"""用中文总结这次数仓建模任务，3-5 个要点，面向业务读者（少术语，多结论）。

建模目标: {state['modeling_goal']}
分层产出: ODS={len(plan.ods_designs or [])}, DIM={len(plan.dim_designs or [])}, DWD={len(plan.dwd_designs or [])}, DWS={len(plan.dws_designs or [])}, ADS={len(plan.ads_designs or [])}
任务状态: {status_value}
执行成功数: {sum(1 for e in executions if e.success)}/{len(executions)}
DML 装载语句数: {dml_count}
目标表实际行数: {total_rows}
校验失败数: {failed_validations}
具体校验错误数: {validation_error_count}
质量检查失败/异常: {quality.get('failed', 0)}/{quality.get('errored', 0)}
告警数: {len(warnings)}
错误数: {len(errors)}

当“是否按单条错误容错继续”为“是”时，应表述为“已生成结果，带 1 项非阻断提示”，不要写成任务失败；但仍不得声称该提示已经被修复。
除上述单条非阻断提示外，除非目标表实际行数为正且所有校验/质量检查通过，否则禁止宣称端到端成功或数据已正确物化——行数为 0 必须明确指出"未产出可用数据"。"""
        narrative = await llm.chat(
            "你是中文数据工程报告撰写助手。只输出中文要点，每行以 '- ' 开头，不要输出其他内容。",
            summary_prompt,
            token_kind="report",
        )
        sections.insert(0, f"# 结果摘要\n{narrative}\n")
    except Exception:
        sections.insert(0, f"# 结果摘要\n{deterministic_summary}\n")

    report = "\n".join(sections)
    state["construction_report"] = report
    state["plain_report"] = build_plain_report(state, now=now)

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "generate_report")

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "generate_report",
        "action": "completed",
        "timestamp": now.isoformat(),
    }]

    return state


async def export_kg(state: AgentState) -> AgentState:
    """Export the knowledge graph to Neo4j and generate data asset map.

    Creates nodes and relationships from the entire task:
    - Source tables → ODS tables → DWD → DWS → ADS (lineage)
    - DIM tables and their dependencies
    - Metric definitions and their sources
    - DAG task nodes
    - Clarification items linked to resolved concepts
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.KG_EXPORT

    plan = state.get("design_plan")
    if plan is None:
        return state

    # Write to Neo4j via GraphStore
    try:
        from wren_agent.tools.neo4j_store import GraphStore
        gs = GraphStore()

        # Collect runtime information to enrich the KG beyond static design
        runtime_info = {
            "execution_results": state.get("execution_results", []),
            "quality_checks": state.get("quality_checks", {}),
            "schema_drifts": state.get("schema_drifts", []),
            "clarifications": state.get("clarifications", []),
            "incremental_strategies": state.get("incremental_strategies", []),
            "node_timeline": state.get("node_timeline", []),
            "explorations": state.get("explorations", {}),
        }

        await gs.create_modeling_task_subgraph(
            state["task_id"], plan, runtime_info=runtime_info
        )
        await gs.close()
        logger.info(f"KG subgraph persisted to Neo4j for task {state['task_id']}")
    except Exception as e:
        logger.warning(f"Failed to write KG to Neo4j: {e}")

    # Generate data asset map (visualization-friendly JSON) from the graph

    # Generate asset map summary
    node_counts = {
        "source_tables": len(plan.ods_designs or []),
        "ods_tables": len(plan.ods_designs or []),
        "dim_tables": len(plan.dim_designs or []),
        "dwd_tables": len(plan.dwd_designs or []),
        "dws_tables": len(plan.dws_designs or []),
        "ads_tables": len(plan.ads_designs or []),
        "metrics": sum(len(a.metrics) for a in (plan.ads_designs or [])),
    }
    total_nodes = sum(node_counts.values())
    total_edges = (
        node_counts["ods_tables"]  # source → ODS
        + node_counts["dwd_tables"]  # ODS → DWD
        + node_counts["dws_tables"]  # DWD → DWS
        + node_counts["ads_tables"]  # DWS → ADS
        + node_counts["dim_tables"]  # FACT → DIM
    )

    asset_map = {
        "task_id": state["task_id"],
        "generated_at": now.isoformat(),
        "nodes": node_counts,
        "total_nodes": total_nodes,
        "total_relationships": total_edges,
        "layers": {
            "ODS": [
                {"table": o.ods_table, "source": o.source_table, "comment": "ODS 原始数据层表"}
                for o in (plan.ods_designs or [])
            ],
            "DIM": [
                {"table": d.dim_table, "key": d.natural_key, "comment": "公共维度表"}
                for d in (plan.dim_designs or [])
            ],
            "DWD": [
                {"table": d.fact_table, "grain": d.grain, "comment": "明细事实层表"}
                for d in (plan.dwd_designs or [])
            ],
            "DWS": [
                {"table": d.summary_table, "grain": d.grain, "comment": "公共汇总层表"}
                for d in (plan.dws_designs or [])
            ],
            "ADS": [
                {"table": a.ads_table, "metrics": [m["name"] for m in a.metrics], "comment": "应用数据层表"}
                for a in (plan.ads_designs or [])
            ],
        },
    }

    import json
    state["data_asset_map"] = json.dumps(asset_map, indent=2, ensure_ascii=False, default=str)
    state["kg_export_id"] = f"kg_{state['task_id']}"

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "export_kg")

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "export_kg",
        "action": "completed",
        "graph_nodes": total_nodes,
        "graph_edges": total_edges,
        "timestamp": now.isoformat(),
    }]

    return state


def _collect_used_tables(plan) -> set[str]:
    """Tables the design actually references (sources and targets)."""
    used: set[str] = set()
    if plan is None:
        return used
    for ods in _list(plan, "ods_designs"):
        if _get(ods, "source_table", ""):
            used.add(_get(ods, "source_table"))
        if _get(ods, "ods_table", ""):
            used.add(_get(ods, "ods_table"))
    for dwd in _list(plan, "dwd_designs"):
        used.update(_list(dwd, "source_ods_tables"))
        if _get(dwd, "fact_table", ""):
            used.add(_get(dwd, "fact_table"))
    for dws in _list(plan, "dws_designs"):
        if _get(dws, "source_dwd_table", ""):
            used.add(_get(dws, "source_dwd_table"))
        used.update(_list(dws, "source_tables"))
        if _get(dws, "summary_table", ""):
            used.add(_get(dws, "summary_table"))
    for ads in _list(plan, "ads_designs"):
        used.update(_list(ads, "source_dws_tables"))
        if _get(ads, "ads_table", ""):
            used.add(_get(ads, "ads_table"))
    used.discard("")
    return used


def _calculate_duration(start: datetime | None, end: datetime) -> str:
    """Calculate human-readable duration."""
    if start is None:
        return "N/A"
    delta = end - start
    hours = delta.total_seconds() // 3600
    minutes = (delta.total_seconds() % 3600) // 60
    seconds = delta.total_seconds() % 60
    if hours > 0:
        return f"{int(hours)}h {int(minutes)}m"
    elif minutes > 0:
        return f"{int(minutes)}m {int(seconds)}s"
    else:
        return f"{int(seconds)}s"


# ── Failure report (plain language, no LLM) ─────────────────────
# Every fail-fast route in graph.py funnels FAILED tasks into
# generate_report, which delegates here. The same builder is reused by
# GET /tasks/{id}/report for historical failed tasks whose run predates
# the route change, so it must tolerate both in-graph pydantic objects
# and the raw dicts returned by pg_store.load_task_artifacts.

def _getv(obj, key, default=None):
    """dict/getattr dual accessor for mixed pydantic/dict data."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# node name → layman label (mirrors dashboard/src/utils/failureHints.js)
_NODE_LABELS = {
    "receive_task": "接收建模任务",
    "blueprint_match": "匹配历史建模蓝图",
    "schema_explore_light": "初步探索源数据表",
    "detect_sensitive_fields": "识别敏感字段",
    "confirm_sensitive": "确认敏感字段",
    "human_confirm_deep": "确认深度探索范围",
    "deep_explore": "深度探索源数据",
    "grill_clarifications": "生成业务确认问题",
    "wait_human": "等待人工答复",
    "detect_schema_drifts": "检测源表结构变更",
    "generate_drift_fixes": "生成结构变更修复方案",
    "design_ods": "设计 ODS 层",
    "design_dim": "设计维度层（DIM）",
    "design_dwd": "设计明细层（DWD）",
    "design_dws": "设计汇总层（DWS）",
    "design_ads": "设计应用层（ADS）",
    "harness_validate": "自动检查设计稿",
    "design_revise": "修订设计稿",
    "human_approval": "人工审批",
    "process_approval": "处理审批结果",
    "dag_deploy": "部署调度任务",
    "execute_tasks": "执行数据装载",
    "classify_errors": "分析执行错误",
    "retry_execution": "重试执行",
    "final_validate": "最终校验",
    "error_handling": "错误处理",
    "quality_monitor": "数据质量检查",
    "generate_report": "生成报告",
    "export_kg": "导出知识图谱",
}

# phase (value str) → (阶段名, 说明)
_PHASE_LABELS = {
    "init": ("任务初始化", "刚接收任务"),
    "explore": ("数据探索", "正在查看源数据库里有哪些表"),
    "schema_explore_light": ("数据探索", "正在查看源数据库里有哪些表"),
    "deep_explore": ("数据探索", "正在深入了解源表结构和样例数据"),
    "clarify": ("业务确认", "正在向业务人员确认口径"),
    "grill": ("业务确认", "正在向业务人员确认口径"),
    "design": ("数仓设计", "正在设计数仓分层方案"),
    "harness_validate": ("设计稿自动检查", "正在对设计稿做自动检查"),
    "approval": ("人工审批", "等待人工审批设计稿"),
    "human_approval": ("人工审批", "等待人工审批设计稿"),
    "deploy": ("任务部署", "正在把任务部署到调度平台"),
    "dag_deploy": ("任务部署", "正在把任务部署到调度平台"),
    "execute": ("数据执行", "正在运行任务装载数据"),
    "execute_tasks": ("数据执行", "正在运行任务装载数据"),
    "final_validate": ("最终校验", "正在做最终校验"),
    "quality": ("数据质量检查", "正在做数据质量检查"),
    "report": ("报告生成", "正在生成报告"),
}

# ordered substring rules: technical error → plain-language explanation
_ERROR_RULES = [
    ("Schema exploration produced no tables",
     "在源数据库中没有找到可用的数据表。常见原因：表名过滤条件过严、连接的不是预期的库，或该库里确实没有表。"),
    ("Critical design invariants failed",
     "自动检查设计稿时发现了必须修正的问题（详见下方问题清单），系统已停止继续执行。"),
    ("auto mode: execution errors persisted",
     "自动模式下系统已尝试自动修复多轮，但执行错误仍未消除，需要人工介入处理。"),
    ("Missing design plan",
     "设计阶段没有产出可检查的方案。"),
    ("no SQL artifacts",
     "设计阶段没有生成任何建表或数据装载 SQL。"),
    ("Breaking schema drift",
     "源表结构发生了不兼容变更，且该变更未获批准。为安全起见，任务已停止。"),
    ("GRAPH_RECURSION_LIMIT",
     "任务内部重试次数达到系统上限，已被安全停止。"),
    ("insufficient_warehouse",
     "现有数仓里没有足够的基础表来支撑这次建模目标。"),
]


def translate_error(message: str) -> str:
    """Translate a technical error into plain language (exported for reuse)."""
    text = str(message)
    for needle, plain in _ERROR_RULES:
        if needle.lower() in text.lower():
            return plain
    return f"{text}（技术细节）"


def _phase_label(phase) -> tuple[str, str]:
    value = getattr(phase, "value", phase) or ""
    return _PHASE_LABELS.get(str(value), (str(value) or "未知阶段", ""))


def _suggestions_for_phase(phase) -> list[str]:
    value = str(getattr(phase, "value", phase) or "")
    if "explore" in value or value == "init":
        return [
            "检查源数据库的连接配置是否正确（地址、账号、库名）。",
            "检查建库时填写的表名过滤条件是否过严。",
            "确认任务选择的模式与数据源匹配：已有数仓选「数仓探索」，原始业务库选「业务库探索」。",
            "确认后可重新发起任务。",
        ]
    if "validate" in value or "design" in value:
        return [
            "查看下方「具体问题清单」，了解设计稿哪些地方未通过自动检查。",
            "可重新发起任务，系统会在自动修订后再次检查。",
            "若多次仍失败，建议用人工审批模式运行，由设计人员介入确认。",
        ]
    if "approval" in value:
        return [
            "改用人工审批模式重新运行，由负责人逐项确认后再继续。",
        ]
    if "deploy" in value or "execute" in value or "quality" in value:
        return [
            "请联系管理员检查调度平台与目标数仓环境是否可用。",
            "环境恢复后可通过「恢复」按钮从断点继续，无需从头再来。",
        ]
    return [
        "可尝试重新运行任务；若反复失败，请联系管理员排查。",
    ]


def _designed_table_names(plan) -> list[str]:
    names: list[str] = []
    if plan is None:
        return names
    for designs_key, table_key in (
        ("ods_designs", "ods_table"),
        ("dim_designs", "dim_table"),
        ("dwd_designs", "fact_table"),
        ("dws_designs", "summary_table"),
        ("ads_designs", "ads_table"),
    ):
        for design in (_getv(plan, designs_key, []) or []):
            table = _getv(design, table_key, "")
            if table:
                names.append(str(table))
    return names


def _has_rejected_breaking_drift(state) -> bool:
    """A breaking schema drift fix was rejected in the approval stage."""
    if state.get("approval_context") != "schema_drift":
        return False
    for item in state.get("approval_items", []) or []:
        artifact_type = _getv(item, "artifact_type", "")
        status = _getv(item, "status", "")
        status_value = getattr(status, "value", status)
        if artifact_type == "drift_fix" and status_value == "rejected":
            return True
    return False


def build_failure_report(state) -> str:
    """Plain-language failure report for a FAILED task (no LLM involved).

    Accepts an AgentState or the persisted dict from load_task_artifacts.
    """
    now = datetime.now(timezone.utc)
    phase = state.get("phase", "")
    phase_name, phase_desc = _phase_label(phase)
    errors = [str(e) for e in (state.get("errors") or [])]
    validations = state.get("validation_results") or []
    timeline = state.get("node_timeline") or []
    plan = state.get("design_plan")
    goal = state.get("modeling_goal", "") or "（未记录）"

    try:
        duration = _calculate_duration(state.get("created_at"), now)
    except TypeError:
        duration = "N/A"

    sections: list[str] = []
    sections.append("# 任务执行报告（未完成）\n")
    sections.append(f"**Task ID**: {state.get('task_id', '')}")
    sections.append(f"**建模目标**: {goal}")
    sections.append("**状态**: 未完成")
    sections.append(f"**耗时**: {duration}")
    sections.append(f"**停在阶段**: {phase_name}")
    sections.append("")

    # One-sentence summary
    sections.append("## 一句话总结\n")
    summary = f"本次任务目标是「{goal}」，在「{phase_name}」阶段停止"
    if phase_desc:
        summary += f"（{phase_desc}）"
    summary += "，未能完成全部建设。以下是具体情况与建议。"
    sections.append(summary)
    sections.append("")

    # What went wrong
    sections.append("## 出了什么问题\n")
    if errors:
        seen: set[str] = set()
        for err in errors:
            plain = translate_error(err)
            if plain not in seen:
                seen.add(plain)
                sections.append(f"- {plain}")
    else:
        sections.append("- 系统未记录具体错误信息。")
    sections.append("")

    # What was completed
    sections.append("## 已完成的工作\n")
    done_labels = [
        _NODE_LABELS.get(str(_getv(e, "node", "")), str(_getv(e, "node", "")))
        for e in timeline
        if _getv(e, "status") == "completed"
    ]
    if done_labels:
        sections.append("任务已走完以下步骤：")
        for label in done_labels:
            sections.append(f"- {label}")
    else:
        sections.append("任务在早期阶段停止，尚未完成可记录的步骤。")
    tables = _designed_table_names(plan)
    if tables:
        sections.append("")
        sections.append("已设计出的表（未部署或未通过检查）：")
        for t in tables:
            sections.append(f"- `{t}`")
    sections.append("")

    # Detailed problem list
    failure_rows: list[tuple[str, str]] = []
    for v in validations:
        for f in (_getv(v, "failures", []) or []):
            failure_rows.append((
                str(_getv(f, "check", "unknown")),
                str(_getv(f, "detail", "")),
            ))
    if failure_rows:
        sections.append("## 具体问题清单\n")
        sections.append("| 检查项 | 问题说明 |")
        sections.append("|---|---|")
        for check, detail in failure_rows[:20]:
            sections.append(f"| {check} | {detail} |")
        if len(failure_rows) > 20:
            sections.append(f"| … | 其余 {len(failure_rows) - 20} 项从略 |")
        sections.append("")

    # Suggestions
    sections.append("## 建议下一步\n")
    for s in _suggestions_for_phase(phase):
        sections.append(f"- {s}")
    sections.append("")

    return "\n".join(sections)
