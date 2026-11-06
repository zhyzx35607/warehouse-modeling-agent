"""Human expert grilling node — generates ClarificationItems via LLM + rules."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from wren_agent.models.task import ClarificationItem, ClarificationStatus, TaskPhase
from wren_agent.state import AgentState

logger = logging.getLogger(__name__)


async def grill_clarifications(state: AgentState) -> AgentState:
    """Generate ClarificationItems using LLM + rule-based analysis.

    First runs rule-based detection (field types, relationships, enum fields),
    then feeds everything to the LLM with the grill-expert skill for deeper analysis.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.GRILL_CLARIFICATIONS

    clarifications = state.get("clarifications", [])
    explorations = state.get("explorations", {})

    # ── Field semantics (A-side): authoritative meanings for the explored
    # tables. Covered fields are skipped by the rule detector and surfaced
    # to the LLM grill so established terms are not re-clarified.
    semantics: dict[str, dict[str, dict]] = {}
    try:
        from wren_agent.tools.pg_store import query_field_semantics
        tables = [t.name for exp in explorations.values() for t in exp.tables]
        if tables:
            for row in await query_field_semantics(tables):
                semantics.setdefault(row["table_name"], {})[row["field_name"]] = row
    except Exception as e:
        logger.warning("Field semantics unavailable (%s) — clarifying without dictionary", e)

    # Fallback chain for uncovered fields:
    #   1. vector similarity over embedded dictionary rows (synonyms) — tagged
    #      vector_similar, medium confidence
    #   2. KG fallback (T-14): exact field-name matches harvested by earlier
    #      tasks, preferring same-layer-prefix tables — tagged kg_similar
    try:
        pairs: list[tuple[str, str]] = []
        for exp in explorations.values():
            for table in exp.tables:
                known = semantics.get(table.name, {})
                for f in table.fields:
                    if f.name not in known:
                        pairs.append((table.name, f.name))
        if pairs:
            from wren_agent.tools.pg_store import semantic_field_semantics_fallback

            sem = await semantic_field_semantics_fallback(pairs)
            for t, fields in sem.items():
                semantics.setdefault(t, {}).update(fields)
            if sem:
                logger.info(
                    "[%s] Vector semantics fallback merged %d table/field entries",
                    state.get("task_id", "")[:8], sum(len(v) for v in sem.values()),
                )
            pairs = [(t, f) for t, f in pairs if f not in (sem.get(t) or {})]
        if pairs:
            from wren_agent.nodes.deployment import task_relevant_db_names
            from wren_agent.tools.neo4j_store import kg_field_semantics_fallback

            prefixes = sorted({f"{t.split('_')[0]}_" for t, _ in pairs if "_" in t})
            db_names = sorted(task_relevant_db_names(state))
            fb = await kg_field_semantics_fallback(
                pairs, prefixes, db_names=db_names or None)
            for t, fields in fb.items():
                semantics.setdefault(t, {}).update(fields)
            logger.info(
                "[%s] KG semantics fallback merged %d table/field entries",
                state.get("task_id", "")[:8], sum(len(v) for v in fb.values()),
            )
    except Exception as e:
        logger.warning("Semantics fallback failed: %s", e)
    state["field_semantics"] = semantics

    from wren_agent.state import is_ods_migration_task
    if is_ods_migration_task(state):
        # 1:1 ODS copy does not need metric/enum/date semantics. Generating
        # blocking cards here parks whole-DB migration on wait_human (9fcfcdbb).
        rule_based = []
        llm_items = []
        state["warnings"] = state.get("warnings", []) + [
            "ods_migration: skipped blocking grill (1:1 copy)"
        ]
    else:
        # ── Phase 1: Rule-based detection ───────────────────────────
        rule_based = _rule_detect_clarifications(explorations, clarifications, semantics)

        # ── Phase 2: LLM-powered deep analysis ─────────────────────
        # On recovery, skip LLM re-generation (rule-based detection already
        # covers the known data; avoids burning credits on re-execution).
        recovery_count = state.get("recovery_count", 0)
        if recovery_count > 0:
            logger.info(
                f"[{state['task_id'][:8]}] Recovery: skipping LLM grill, "
                "rule-based only"
            )
            llm_items = []
        else:
            try:
                llm_items = await _llm_generate_clarifications(state)
                if llm_items:
                    rule_based.extend(llm_items)
            except Exception as e:
                state["warnings"] = state.get("warnings", []) + [
                    f"LLM grill failed, using rule-based only: {e}"
                ]
                llm_items = []

    # Merge new clarifications (avoid duplicates by question text)
    existing_questions = {c.question for c in clarifications}
    new_items = [c for c in rule_based if c.question not in existing_questions]

    state["clarifications"] = clarifications + new_items

    # Persist every clarification currently in state, including items created
    # by confirm_sensitive before this node. Recovery can re-execute the node,
    # so conflict on the stable item id is intentionally ignored.
    items_to_persist = state["clarifications"]
    if items_to_persist:
        try:
            from wren_agent.tools.pg_store import get_pg_pool
            pool = get_pg_pool()
            async with pool.acquire() as conn:
                for c in items_to_persist:
                    await conn.execute(
                        "INSERT INTO task_clarifications "
                        "(id, task_id, question, context, candidates, "
                        "impact_level, blocking, status, clarification_type, "
                        "recommended, recommended_reason, answer, answered_at, created_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW()) "
                        "ON CONFLICT (id) DO NOTHING",
                        str(c.id), state["task_id"], c.question, c.context or "",
                        json.dumps(c.candidates or [], ensure_ascii=False),
                        c.impact_level or "medium",
                        c.blocking, c.status.value, c.clarification_type or "",
                        c.recommended, c.recommended_reason or "",
                        c.answer, c.answered_at,
                    )
        except Exception as e:
            logger.warning(f"Failed to persist clarifications to PG: {e}")

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "grill_clarifications")
    if new_items:
        from wren_agent.tools.event_emitter import emit_clarification
        for c in new_items[:3]:
            emit_clarification(state, c.question, len(state["clarifications"]))

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "grill_clarifications",
        "action": "generated",
        "rule_based": len(rule_based) - len(llm_items if 'llm_items' in dir() else []),
        "llm_generated": len(llm_items) if 'llm_items' in dir() else 0,
        "new_clarifications": len(new_items),
        "total_clarifications": len(state["clarifications"]),
        "timestamp": now.isoformat(),
    }]

    return state


# ── Rule-based detection ────────────────────────────────────────


def _rule_detect_clarifications(
    explorations: dict,
    existing: list,
    semantics: dict[str, dict[str, dict]] | None = None,
) -> list[ClarificationItem]:
    """Detect clarification needs from schema structure patterns.

    `semantics` (table -> field -> row) marks fields whose business meaning
    is already known from the field semantic dictionary — those are skipped
    so the human is not asked to re-clarify established terms.
    """
    items: list[ClarificationItem] = []
    semantics = semantics or {}

    def _known(table: str, field: str) -> bool:
        return bool(semantics.get(table, {}).get(field, {}).get("chinese_meaning"))

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            # Numeric metric candidates
            numeric_fields = [
                f for f in table.fields
                if f.data_type and any(
                    t in f.data_type.upper()
                    for t in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "MONEY")
                )
                and not _known(table.name, f.name)
            ]
            for field in numeric_fields[:3]:
                items.append(ClarificationItem(
                    question=f"'{table.name}.{field.name}' 的业务含义是什么？",
                    context=f"数据类型: {field.data_type}。表 '{table.name}' 中的数值字段。",
                    candidates=["度量指标（如金额/数量）", "维度属性（如编码/ID）", "需要转换后使用"],
                    impact_level="high", blocking=True,
                    clarification_type="metric_definition",
                ))

            # Timestamp candidates
            time_fields = [
                f for f in table.fields
                if f.data_type and any(
                    t in f.data_type.upper()
                    for t in ("DATE", "TIME", "TIMESTAMP")
                )
                and not _known(table.name, f.name)
            ]
            for field in time_fields[:2]:
                items.append(ClarificationItem(
                    question=f"'{table.name}.{field.name}' 代表什么时间？",
                    context=f"数据类型: {field.data_type}。确认含义用于增量策略和分区键选择。",
                    candidates=["业务发生时间", "数据更新时间", "记录创建时间"],
                    impact_level="high", blocking=True,
                    clarification_type="metric_definition",
                ))

            # Enum-like fields
            enum_patterns = ["status", "type", "state", "flag", "category", "kind"]
            enum_fields = [
                f for f in table.fields
                if any(pat in f.name.lower() for pat in enum_patterns)
                and f.data_type and any(
                    t in f.data_type.upper() for t in ("VARCHAR", "CHAR", "TINYINT", "SMALLINT", "INT")
                )
                and not _known(table.name, f.name)
            ]
            for field in enum_fields[:2]:
                items.append(ClarificationItem(
                    question=f"'{table.name}.{field.name}' 的枚举值含义是什么？",
                    context=f"状态/类型字段，需要明确每个值的业务含义。",
                    candidates=["请列出枚举值及含义", "不需要特殊处理"],
                    impact_level="medium", blocking=True,
                    clarification_type="business_logic",
                ))

        # Unverified relationships
        for rel in exploration.relationships:
            if not rel.human_confirmed:
                items.append(ClarificationItem(
                    question=f"'{rel.source_table}' 和 '{rel.target_table}' 的关系？",
                    context=f"通过 {rel.source_column} ↔ {rel.target_column} 关联。",
                    candidates=["1:1", "1:N", "N:1", "N:M"],
                    impact_level="high", blocking=True,
                    clarification_type="table_relation",
                ))

    return items


# ── LLM-powered analysis ────────────────────────────────────────


async def _llm_generate_clarifications(state: AgentState) -> list[ClarificationItem]:
    """Use LLM with grill-expert skill to generate deeper clarification questions."""
    from wren_agent.tools.llm_client import get_llm_client, build_system_prompt

    llm = get_llm_client()

    # Build exploration summary for the LLM
    summary = _build_exploration_summary(state)
    goal = state["modeling_goal"]

    # Warehouse mode: surface the system's own knowledge — registered
    # dimensions and metrics — so the agent does not re-clarify established
    # terms (see wayfinder T-07). Registries may be empty (T-02 inventory).
    registry_context = ""
    if state.get("source_mode") == "warehouse":
        registry_context = await _build_registry_context(state)

    # Field semantics (A-side): the dictionary is authoritative — the LLM
    # must not re-clarify fields whose meaning/role is already known.
    semantics_context = _build_semantics_context(state)

    system_prompt = build_system_prompt(
        skill_names=["grill-expert"],
        extra_context=(
            "You are a data warehouse modeling expert AI. "
            "Your job is to identify knowledge gaps and generate clarification questions "
            "that a human expert needs to answer before the data warehouse can be built.\n\n"
            f"MODELING GOAL: {goal}\n\n"
            "EMPTY date/period columns (null_rate≥99%) must NOT be treated as "
            "a usable time grain. Do NOT ask how to extract a month/year from "
            "such a column. Ask which POPULATED date column to use, listing "
            "alternatives from the exploration summary."
        ),
    )

    user_message = f"""Based on the following database exploration results, identify ALL knowledge gaps
that need human clarification. Think about: metric definitions, table relationships,
business logic for status codes, data quality issues, partitioning strategies.

{summary}
{registry_context}
{semantics_context}
Generate clarification items following the **Output Contract** in the system prompt
(grill-expert skill). Return ONLY valid JSON. No markdown, no explanation.
Limit to the 8 most important questions."""

    # A single LLM call can return an EMPTY response (gateway hiccup — task
    # d28431de: grill burned 120s and produced 0 clarifications, so the whole
    # design ran with "No human clarifications available"). Retry once on
    # empty/unparseable output before giving up.
    for attempt in range(2):
        try:
            response = await llm.generate_json(
                system_prompt, user_message, token_kind="grill",
            )
            # Extract JSON from potential markdown wrapping
            json_match = re.search(r'\{.*\}', response or "", re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                clar_list = data.get("clarifications", [])
                if clar_list:
                    items = []
                    for c in clar_list[:10]:
                        candidates = c.get("candidates", []) or []
                        items.append(ClarificationItem(
                            question=c.get("question", ""),
                            context=c.get("context", ""),
                            candidates=candidates,
                            recommended=_normalize_recommended(c.get("recommended"), candidates),
                            recommended_reason=str(c.get("recommended_reason", "") or ""),
                            impact_level=c.get("impact_level", "medium"),
                            blocking=c.get("blocking", True),
                            clarification_type=c.get("clarification_type", ""),
                        ))
                    return items
                break  # valid empty list — no clarifications needed
            if attempt == 0:
                logger.warning(
                    "[%s] grill LLM returned empty/unparseable response — retrying once",
                    state.get("task_id", "")[:8],
                )
        except Exception as exc:
            # Transient network hiccup: retry once; deterministic failures fall
            # through to the rule-based fallback.
            if attempt == 0 and not isinstance(exc, (TypeError, ValueError, KeyError)):
                logger.warning(
                    "[%s] grill LLM call failed (%s) — retrying once",
                    state.get("task_id", "")[:8], type(exc).__name__,
                )
                continue
            break
    # LLM failure is non-fatal — rule-based detection already ran
    return []


def _normalize_recommended(raw: Any, candidates: list[str]) -> str | None:
    """Normalize the LLM's `recommended` value into an answer string.

    Tolerates common LLM drift: missing/empty values fall back to the first
    candidate, integer or digit-string values are treated as an index into
    candidates, and loose text is matched back to the canonical candidate
    (case/whitespace-insensitive). Unmatched text is kept as a free-text
    recommendation.
    """
    fallback = candidates[0] if candidates else None
    if raw is None or isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return candidates[raw] if 0 <= raw < len(candidates) else fallback
    s = str(raw).strip()
    if not s:
        return fallback
    if s.isdigit() and candidates:
        i = int(s)
        return candidates[i] if i < len(candidates) else fallback
    for cand in candidates:
        if cand.strip().lower() == s.lower():
            return cand
    return s


# The grill prompt only needs the goal-relevant slice of the schema — the
# full dump (150+ tables × every field) blew it past 100K chars (task
# c0f31ae0: grill_clarifications user_message 100K+ truncated) for answers
# that never use more than the relevant subset.
_GRILL_MAX_TABLES = 60


def _build_exploration_summary(state: AgentState) -> str:
    """Build a text summary of schema exploration for the LLM.

    Tables are sampled by goal relevance (with a per-layer floor) so the
    prompt stays bounded while dim join context is never starved.
    """
    explorations = state.get("explorations", {})
    if not explorations:
        return "(No schema explorations available)"

    from wren_agent.nodes.design_layers import (
        _goal_keywords,
        _select_relevant_tables,
    )
    from wren_agent.tools.column_profile import (
        format_null_mark, is_profile_candidate, is_unusable_grain,
    )

    selected = _select_relevant_tables(
        explorations, _goal_keywords(state.get("modeling_goal") or ""),
        _GRILL_MAX_TABLES,
    )
    parts = []
    emitted: dict[str, int] = {}
    for db_id, table in selected:
        if db_id not in emitted:
            emitted[db_id] = 0
            parts.append(f"Database: {db_id}")
        if emitted[db_id] >= 25:
            continue
        emitted[db_id] += 1
        fields_desc = ", ".join(
            f"{f.name}({f.data_type})"
            + (" [PK]" if f.primary_key else "")
            for f in table.fields[:15]
        )
        est = getattr(table, "row_count_estimate", None)
        est_txt = "?" if est is None else f"{est:,}"
        empty_mark = " [EMPTY]" if est == 0 else ""
        parts.append(f"  Table {table.name}: {est_txt} rows{empty_mark}")
        parts.append(f"    Fields: {fields_desc}")
        empty_dates = []
        populated_dates = []
        for field in table.fields:
            if not is_profile_candidate(field):
                continue
            ratio = getattr(field, "null_ratio", None)
            mark = format_null_mark(ratio)
            if not mark:
                continue
            if is_unusable_grain(ratio):
                empty_dates.append(f"{field.name}{mark}")
            else:
                populated_dates.append(f"{field.name}{mark}")
        if empty_dates:
            parts.append(
                "    EMPTY date columns (do NOT extract grain from these): "
                + "; ".join(empty_dates)
            )
        if populated_dates:
            parts.append("    Populated date columns: " + "; ".join(populated_dates))
        if table.primary_keys:
            parts.append(f"    PK: {table.primary_keys}")

    for db_id, exp in explorations.items():
        total = len(exp.tables or [])
        shown = emitted.get(db_id, 0)
        if total > shown:
            parts.append(f"  … {total - shown} more tables in {db_id} omitted (sampled)")

    # Relationships (full — they are small and high-value for grilling)
    for exp in explorations.values():
        if exp.relationships:
            parts.append("  Relationships:")
            for rel in exp.relationships:
                parts.append(
                    f"    {rel.source_table}.{rel.source_column} ↔ "
                    f"{rel.target_table}.{rel.target_column}"
                    + (f" (verified: {rel.relationship_type.value})" if rel.verified else " (unverified)")
                )

    # Sensitive fields
    sensitive = state.get("sensitive_fields", [])
    if sensitive:
        parts.append("\nDetected sensitive fields:")
        for sf in sensitive:
            parts.append(f"  {sf.table_name}.{sf.field_name} → {sf.sensor_type.value}")

    return "\n".join(parts)


def _build_semantics_context(state: AgentState) -> str:
    """Serialize the field semantic dictionary for the LLM grill prompt.

    The dictionary (loaded in generate_clarifications into state) is
    authoritative — the LLM is told not to re-clarify these fields.
    """
    semantics = state.get("field_semantics") or {}
    lines = []
    for table, fields in semantics.items():
        for field, meta in fields.items():
            if not (meta or {}).get("chinese_meaning"):
                continue
            role = meta.get("field_role") or ""
            enum = meta.get("enum_values") or []
            enum_txt = f" 枚举: {'、'.join(str(e) for e in enum)}" if enum else ""
            prov = ""
            if meta.get("source") == "kg_similar" and meta.get("kg_from"):
                prov = f" (来自相似表 {meta['kg_from']})"
            elif meta.get("source") == "vector_similar" and meta.get("sem_from"):
                prov = f" (语义相似 {meta['sem_from']}, sim={meta.get('similarity')})"
            lines.append(f"  {table}.{field}: {meta['chinese_meaning']} [{role}]{enum_txt}{prov}")
    if not lines:
        return ""
    return (
        "\nKnown field semantics (authoritative — do NOT generate clarification "
        "questions for these fields):\n" + "\n".join(lines)
    )


async def _build_registry_context(state: AgentState) -> str:
    """Warehouse-mode grill context: registered dimensions and metrics.

    These are the system's own established terms — surfacing them keeps the
    agent from re-clarifying definitions that already exist (T-07). Registries
    may be empty (T-02 inventory found 0 rows); the section is then omitted.
    Entries are scoped to the task's layer databases (dimensions → dim layer,
    metrics → dws layer) and narrowed to those semantically related to the
    modeling goal, so other sources' terms never pollute this task's questions.
    """
    from wren_agent.nodes.deployment import layer_relevant_db_names
    from wren_agent.tools.pg_store import query_dimension_registry, query_metric_registry

    goal = state.get("modeling_goal") or ""
    parts = []
    try:
        dim_dbs = sorted(layer_relevant_db_names(state, {"dim"}))
        dims = await query_dimension_registry(db_names=dim_dbs or None, goal=goal or None)
        if dims:
            parts.append("\nRegistered dimensions (dimension_registry):")
            for d in dims[:20]:
                parts.append(
                    f"  {d.get('dim_name')} — table={d.get('dim_table')}, "
                    f"grain={d.get('grain') or '?'}"
                )
    except Exception:
        pass

    try:
        dws_dbs = sorted(layer_relevant_db_names(state, {"dws"}))
        metrics = await query_metric_registry(db_names=dws_dbs or None, goal=goal or None)
        if metrics:
            parts.append("\nRegistered metrics (metric_registry):")
            for m in metrics[:30]:
                parts.append(
                    f"  {m.get('metric_name')} ({m.get('metric_type')}) — "
                    f"table={m.get('source_table')}, grain={m.get('grain') or '?'}"
                )
    except Exception:
        pass

    return "\n".join(parts)


async def wait_human(state: AgentState) -> AgentState:
    """Interrupt: wait for human to answer clarifications.

    Calls LangGraph interrupt() to pause execution.
    Human resumes via API → Command(resume=...) → graph continues.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    # PG is the source of truth for answers. Always sync before deciding to
    # pause: users can submit several cards while the graph is transitioning
    # between two wait_human executions, not only while recovering a crash.
    await _sync_clarification_answers_from_pg(state)

    pending_blocking = [
        c for c in state.get("clarifications", [])
        if hasattr(c, "blocking") and c.blocking and
           c.status == "pending"
    ]
    pending_nonblocking = [
        c for c in state.get("clarifications", [])
        if (not hasattr(c, "blocking") or not c.blocking) and
           c.status == "pending"
    ]

    from wren_agent.state import is_auto_mode, is_ods_migration_task
    if is_ods_migration_task(state) and pending_blocking:
        state["warnings"] = state.get("warnings", []) + [
            f"ods_migration: auto-skipped {len(pending_blocking)} blocking "
            "clarification(s) (1:1 copy)"
        ]
        return await _auto_answer_clarifications(state, pending_blocking, now)

    # Non-blocking questions remain available for optional answers, but must
    # never stop the modeling pipeline (see grill-expert contract).
    if len(pending_blocking) == 0:
        state["pending_human_input"] = False
        state["human_input_type"] = ""
        state["status"] = "designing"
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "wait_human",
            "action": "skipped_no_blocking",
            "blocking_count": 0,
            "non_blocking_count": len(pending_nonblocking),
            "timestamp": now.isoformat(),
        }]
        # A previous process/request may have persisted every answer but left
        # a stale pause row and grilling status behind. Converge both stores
        # before routing to design so recovery also repairs existing tasks.
        try:
            from wren_agent.tools.pg_store import clear_task_pause
            await clear_task_pause(state["task_id"])
        except Exception:
            logger.debug("clear stale clarification pause failed", exc_info=True)
        from wren_agent.engine import update_task_pg_status
        await update_task_pg_status(state["task_id"], "designing", "design_ods")
        return state

    # ── Auto mode: answer everything with the LLM recommendation, never pause ──
    if is_auto_mode(state):
        return await _auto_answer_clarifications(
            state, pending_blocking + pending_nonblocking, now
        )

    state["status"] = "grilling"
    state["pending_human_input"] = True
    state["human_input_type"] = "clarification"

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "wait_human",
        "action": "awaiting_clarifications",
        "blocking_count": len(pending_blocking),
        "non_blocking_count": len(pending_nonblocking),
        "timestamp": now.isoformat(),
    }]

    # Emit interrupt event to WebSocket
    from wren_agent.tools.event_emitter import emit_interrupt
    emit_interrupt(state, "clarification", {
        "blocking_count": len(pending_blocking),
        "non_blocking_count": len(pending_nonblocking),
        "questions": [
            {"id": str(c.id), "question": c.question[:200]}
            for c in pending_blocking[:5]
        ],
    })

    # Update PG status before pausing
    from wren_agent.engine import (
        _task_pauses, update_task_pg_status, wait_for_resume,
    )
    await update_task_pg_status(state["task_id"], "grilling", "wait_human")

    # ACTUAL pause: register the pause in PG (migration 010) so ANY engine
    # instance can resume this task, then wait — in-process event fast path
    # + PG poll for cross-engine decisions.
    from wren_agent.tools.pg_store import clear_task_pause, pause_task
    await pause_task(state["task_id"], "clarification")
    pause_ev = asyncio.Event()
    _task_pauses[state["task_id"]] = pause_ev

    # Close the race between the first PG sync and pause registration. An
    # answer submitted inside that window updates task_clarifications before a
    # task_pauses row exists, so resume_task cannot wake us. A second sync now
    # observes that answer; answers arriving after this point wake the event or
    # are observed by the PG poll.
    await _sync_clarification_answers_from_pg(state)
    still_pending_blocking = any(
        c.blocking and c.status == "pending"
        for c in state.get("clarifications", [])
    )
    payload = (
        await wait_for_resume(state["task_id"], pause_ev, timeout=1800)
        if still_pending_blocking else {}
    )

    # After resume — apply the decision from PG (cross-engine source of truth)
    if payload:
        cid = payload.get("cid", "")
        answer = payload.get("answer", "")
        if cid and answer:
            for c in state.get("clarifications", []):
                if str(c.id) == cid and hasattr(c, "status"):
                    c.status = type(c.status)("answered") if hasattr(type(c.status), '__call__') else "answered"
                    c.answer = answer
                    from datetime import datetime as dt2
                    c.answered_at = dt2.now(timezone.utc)
        state["pending_human_input"] = False

    # Sync ALL PG-persisted answers into state — the resume API may batch
    # several answers (e.g. bulk skip) while wait_human only reads one
    # payload; without this the state keeps stale pending items and
    # route_after_human_response loops back to wait_human forever.
    await _sync_clarification_answers_from_pg(state)

    has_pending_blocking = any(
        c.blocking and c.status == "pending"
        for c in state.get("clarifications", [])
    )
    state["pending_human_input"] = has_pending_blocking
    state["human_input_type"] = "clarification" if has_pending_blocking else ""

    # Clean up and reset status
    _task_pauses.pop(state["task_id"], None)
    try:
        await clear_task_pause(state["task_id"])
    except Exception:
        logger.debug("clear_task_pause failed", exc_info=True)
    if has_pending_blocking:
        # route_after_human_response will immediately re-enter wait_human.
        state["status"] = "grilling"
        await update_task_pg_status(state["task_id"], "grilling", "wait_human")
    else:
        state["status"] = "designing"
        await update_task_pg_status(state["task_id"], "designing", "design_ods")

        # Emit status change so frontend hides the interrupt banner
        from wren_agent.tools.event_emitter import emit_status
        emit_status(state, "designing", "grilling")

    return state


async def _auto_answer_clarifications(
    state: AgentState, pending: list[ClarificationItem], now: datetime
) -> AgentState:
    """Auto mode: resolve every pending clarification without pausing.

    Answer priority: LLM `recommended` → first candidate → SKIPPED with a
    warning (free-text questions are never answered with fabricated text).
    """
    from wren_agent.engine import persist_clarification_answer, update_task_pg_status
    from wren_agent.tools.event_emitter import emit, Events

    answered, skipped = [], []
    for c in pending:
        answer = c.recommended or (c.candidates[0] if c.candidates else None)
        if answer:
            c.status = ClarificationStatus.ANSWERED
            c.answer = answer
            c.answered_at = now
            await persist_clarification_answer(state["task_id"], str(c.id), answer)
            emit(state, Events.CLARIFICATION_ANSWERED, {
                "id": str(c.id),
                "question": c.question[:200],
                "answer": answer[:200],
                "auto": True,
            })
            answered.append(c)
        else:
            # Free-text question with no candidates — skip, never fabricate
            c.status = ClarificationStatus.SKIPPED
            await persist_clarification_answer(
                state["task_id"], str(c.id), None, status="skipped"
            )
            skipped.append(c)

    if skipped:
        state["warnings"] = state.get("warnings", []) + [
            f"auto mode skipped {len(skipped)} unanswerable clarification(s): "
            + "; ".join(c.question[:60] for c in skipped[:3])
        ]

    state["pending_human_input"] = False
    state["status"] = "designing"
    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "wait_human",
        "action": "auto_answered",
        "answered": len(answered),
        "skipped": len(skipped),
        "timestamp": now.isoformat(),
    }]
    await update_task_pg_status(state["task_id"], "designing", "design_ods")

    logger.info(
        f"[{state['task_id'][:8]}] Auto mode: {len(answered)} clarification(s) "
        f"auto-answered, {len(skipped)} skipped"
    )
    return state


async def _sync_clarification_answers_from_pg(state: AgentState) -> None:
    """On recovery, pull any answers persisted to PG before the crash.

    If the crash happened AFTER the human answered via API but BEFORE the
    wait_human node finished processing (e.g. during the asyncio.Event.set()
    wakeup), the PG table has the answer but the checkpointed state may not.
    This sync prevents asking the same question twice.
    """
    try:
        from wren_agent.tools.pg_store import get_pg_pool
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, answer, status FROM task_clarifications "
                "WHERE task_id=$1 AND status IN ('answered', 'skipped')",
                state["task_id"],
            )
        for row in rows:
            for c in state.get("clarifications", []):
                if str(c.id) == str(row["id"]) and hasattr(c, "status"):
                    if row["status"] == "answered":
                        c.status = (
                            type(c.status)("answered")
                            if hasattr(type(c.status), "__call__")
                            else "answered"
                        )
                        c.answer = row["answer"]
                    elif row["status"] == "skipped":
                        c.status = (
                            type(c.status)("skipped")
                            if hasattr(type(c.status), "__call__")
                            else "skipped"
                        )
                        c.answer = None
    except Exception:
        pass  # Non-blocking — recovery continues regardless
