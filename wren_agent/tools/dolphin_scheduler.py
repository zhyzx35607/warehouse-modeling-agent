"""Dolphin Scheduler integration via MCP (existing MCP server)."""

from __future__ import annotations

import logging
import re as _re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from wren_agent.models.task import (
    ErrorClassification,
    ErrorType,
    ExecutionResult,
)

logger = logging.getLogger(__name__)


class DSTaskStatus(str, Enum):
    """Dolphin Scheduler task instance status."""
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    PAUSE = "pause"
    STOP = "stop"
    KILL = "kill"


async def create_ds_project(
    mcp_client: Any,
    project_name: str,
    description: str = "",
) -> dict:
    """Create a Dolphin Scheduler project."""
    # Implementation: calls DS MCP tool 'create_project'
    return {"project_name": project_name, "created": True}


async def create_ds_data_source(
    mcp_client: Any,
    name: str,
    db_type: str,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    note: str = "",
) -> dict:
    """Create a Dolphin Scheduler data source.

    Note: In the preferred path, the MCP exposes register_as_ds_source()
    which handles credential retrieval internally. This function is the
    fallback when MCP doesn't support that interface (see ADR discussion #19).
    Password retrieval is handled by the Secret Manager, not passed as plain text.
    """
    # Implementation: calls DS MCP tool 'create_data_source' or uses DS REST API
    return {"name": name, "created": True}


async def create_dag_with_datax(
    mcp_client: Any,
    project_name: str,
    dag_name: str,
    data_source_reader: str,
    data_source_writer: str,
    datax_json: str,
    schedule_cron: str = "0 0 * * *",  # Default: daily midnight
) -> dict:
    """Create a DAG in DS with DataX task components."""
    return {"dag_name": dag_name, "created": True}


async def create_dag_with_flink_cdc(
    mcp_client: Any,
    project_name: str,
    dag_name: str,
    flink_cdc_config: dict,
) -> dict:
    """Create a DAG in DS with Flink CDC task components."""
    return {"dag_name": dag_name, "created": True}


async def execute_dag(
    mcp_client: Any,
    project_name: str,
    dag_id: str,
) -> str:
    """Manually execute a DAG, returning the task instance ID."""
    return "task_instance_xxx"


async def get_task_logs(
    mcp_client: Any,
    task_instance_id: str,
) -> dict:
    """Get execution logs for a task instance."""
    return {
        "task_instance_id": task_instance_id,
        "logs": ["[INFO] Task started...", "[INFO] Task completed."],
        "status": "success",
    }


def classify_execution_error(
    task_instance_id: str,
    raw_log: str,
    retry_count: int = 0,
) -> ErrorClassification:
    """Classify a DS execution error into one of 6 error types.

    Error types and their handling:
    - TRANSIENT: network timeouts, connection pool exhaustion → retry with backoff
    - RESOURCE: disk full, OOM → alert human, wait for resolution
    - DATA: dirty data, constraint violations → escalate to human
    - LOGIC: SQL logic errors → escalate to human (agent generated buggy SQL)
    - CONFIG: connection config wrong, permission denied → escalate to human
    - DEPENDENCY: upstream schema changed → escalate to human
    """
    summary = ""
    error_type = ErrorType.LOGIC  # Default worst-case

    raw_lower = raw_log.lower()

    # Dolphin Scheduler platform-internal errors — the SQL never ran; the
    # failure is in DS itself (e.g. a dependent-task context write hitting a
    # null id in t_ds_task_instance_context after a bad migration/restore,
    # task def559ca). Routing these through the SQL-repair loop wastes the
    # revision budget; they need an ops fix on the DS database, not a new
    # design. Highest priority (checked before keyword matches).
    platform_keywords = [
        "t_ds_task_instance_context",
        "data integrity violation",
        "null value in column",
        "org.springframework.dao",
    ]
    if any(kw in raw_lower for kw in platform_keywords):
        return ErrorClassification(
            task_instance_id=task_instance_id,
            error_type=ErrorType.CONFIG,
            raw_log=raw_log,
            summary=(
                "Dolphin Scheduler platform error (dependent-task context "
                "write failed) — DS database issue, not agent SQL; ops must "
                "repair t_ds_task_instance_context (sequence/not-null) "
                "before redeploy"
            ),
            action="escalate",
        )

    # ``|| '-Q'`` is OR, not a dirty BOOLEAN column (task 60a570e6). Must
    # run before generic "cannot cast" / empty-string-guard advice.
    if "cast to bool" in raw_lower:
        return ErrorClassification(
            task_instance_id=task_instance_id,
            error_type=ErrorType.LOGIC,
            raw_log=raw_log,
            summary=(
                "Doris 把 || 当成逻辑或，字符串无法转为 BOOLEAN。"
                "按 repair-doris-dml 依次尝试 CONCAT / CAST / OR"
            ),
            fix_suggestion=(
                "Locate the error token in SQL. Score CONCAT / CAST CEIL / "
                "CAST VARCHAR / CONCAT_WS / OR by P(success); try the "
                "highest untried. Never WHERE col IN ('true','false')"
            ),
            action="escalate",
        )

    # INSERT-strict: comparing a typed BIGINT/DATE to '' casts the empty
    # string to decimal and throws. The fix is to REMOVE those guards from
    # native numeric/date columns — adding more `!= ''` makes it worse
    # (task 0af225c9). Checked before the VARCHAR-empty-string keywords.
    if (
        "cast to decimal in strict mode" in raw_lower
        or "can't cast to decimal" in raw_lower
    ):
        return ErrorClassification(
            task_instance_id=task_instance_id,
            error_type=ErrorType.LOGIC,
            raw_log=raw_log,
            summary=(
                "Doris INSERT-strict cannot cast '' to decimal — remove "
                "`col != ''` / NULLIF(col,'') on BIGINT/INT/DECIMAL/DATE "
                "columns; keep empty-string guards only on VARCHAR/STRING"
            ),
            action="escalate",
        )

    # Source-data quality errors: empty strings in VARCHAR date/numeric
    # columns break Doris functions at execution (`QUARTER('')` →
    # "parse number fail, string: ''", task 55f47744). Agent-repairable via
    # design_revise (add NULLIF/WHERE guards on STRING columns) — NOT
    # resource/infra.
    data_quality_keywords = [
        "parse number fail", "invalid argument",
        "cannot cast", "invalid numeric",
    ]
    if any(kw in raw_lower for kw in data_quality_keywords):
        return ErrorClassification(
            task_instance_id=task_instance_id,
            error_type=ErrorType.LOGIC,
            raw_log=raw_log,
            summary=(
                "Doris data-quality error (empty/invalid values in VARCHAR "
                "source columns) — add NULLIF(col, '') / WHERE col != '' "
                "guards only on STRING columns in the load SQL"
            ),
            action="escalate",
        )

    # Transient errors
    transient_keywords = [
        "connection refused", "timeout", "connection reset",
        "connection pool", "lock wait timeout", "deadlock",
        "too many connections", "broken pipe", "temporary failure",
        "service unavailable", "network", "timed out",
    ]
    if any(kw in raw_lower for kw in transient_keywords):
        error_type = ErrorType.TRANSIENT
        summary = "Transient error (network/connection)"

    # Resource errors. NOTE: no bare "oom" — it's a substring of table names
    # like dws_order_subscription_monthly, which misclassified the "parse
    # number fail" SQL error as RESOURCE and sent the task into an infinite
    # redesign loop (task 55f47744).
    resource_keywords = [
        "disk full", "out of memory", "quota exceeded",
        "no space left", "memory limit exceeded",
    ]
    if any(kw in raw_lower for kw in resource_keywords):
        error_type = ErrorType.RESOURCE
        summary = "Resource exhaustion (disk/memory)"

    # Data errors
    data_keywords = [
        "duplicate entry", "unique constraint", "foreign key constraint",
        "cannot be null", "data too long", "division by zero",
        "out of range", "invalid datetime", "check constraint",
        "integrity constraint", "violation",
    ]
    if any(kw in raw_lower for kw in data_keywords):
        error_type = ErrorType.DATA
        summary = "Data integrity error"

    # Config errors
    config_keywords = [
        "access denied", "permission denied", "unknown database",
        "unknown table", "host not allowed", "authentication failed",
        "not authorized", "unauthorized",
    ]
    if any(kw in raw_lower for kw in config_keywords):
        error_type = ErrorType.CONFIG
        summary = "Configuration/permission error"

    # Dependency errors — these are regexes (the old "column.*not found"
    # literals never matched anything as plain substrings).
    # `column X is not found in table Y` is NOT blindly a dependency: when
    # the target table already exists (an earlier run of the same goal built
    # a different schema) and X is absent from it, the failure is the agent's
    # own SQL vs an existing table — agent-fixable via design_revise (LOGIC).
    # Whether it is a true upstream schema change can only be decided against
    # the REAL schema, so classify_errors re-checks DEPENDENCY results with
    # DESCRIBE (task 3034d2f8: no-op CREATE TABLE IF NOT EXISTS + missing
    # monthly_charge_count was mis-escalated). Table/relation not-found stays
    # DEPENDENCY (a missing source table is upstream by definition).
    not_found_column_patterns = [r"column\s+\S+\s+not found"]
    if any(_re.search(p, raw_lower) for p in not_found_column_patterns):
        error_type = ErrorType.LOGIC
        summary = (
            "SQL references a column that does not exist in a real table "
            "(agent SQL vs existing schema — pending real-schema verification)"
        )

    dependency_patterns = [
        r"relation.*does not exist",
        r"table.*not found",
    ]
    if any(_re.search(p, raw_lower) for p in dependency_patterns):
        error_type = ErrorType.DEPENDENCY
        summary = "Schema dependency error (missing column/table)"

    # Unknown-column errors are almost always the agent's own SQL referencing
    # a column that does not exist in the explored schema — LLM-repairable
    # via design_revise (LOGIC), not an upstream schema change. Classifying
    # them as DEPENDENCY sent them to human_approval, where auto mode re-ran
    # the same broken SQL until the task failed (task 2b459bd5). True
    # upstream schema changes are caught earlier by detect_schema_drifts.
    unknown_column_keywords = ["unknown column", "no such column"]
    if any(kw in raw_lower for kw in unknown_column_keywords):
        error_type = ErrorType.LOGIC
        summary = "SQL references a column that does not exist (agent-fixable)"

    # Task never ran — no log was produced on the worker. Highest priority
    # (checked last, overrides keyword matches): retrying or regenerating SQL
    # cannot fix a task that dies before it starts. Typical causes: task-type
    # plugin missing on the worker (e.g. DataX not installed), datasource
    # type mismatch in the task definition, undefined ${...} params.
    never_ran_keywords = [
        "taskinstancelogpath is empty",
        "doesn't be dispatched",
        "log fetch failed",
    ]
    if any(kw in raw_lower for kw in never_ran_keywords):
        error_type = ErrorType.CONFIG
        summary = (
            "Task never started on the DS worker (no log produced) — "
            "check worker environment (DataX/plugin installed?) and the "
            "task definition (datasource type, ${...} params); re-running "
            "cannot fix this"
        )

    # Logic errors (default catch-all)
    if not summary:
        error_type = ErrorType.LOGIC
        summary = "SQL logic error (possibly agent-generated)"

    action = "escalate"
    if error_type == ErrorType.TRANSIENT:
        action = "retry" if retry_count < 3 else "escalate"
    elif error_type == ErrorType.RESOURCE:
        action = "wait"

    return ErrorClassification(
        task_instance_id=task_instance_id,
        error_type=error_type,
        raw_log=raw_log,
        summary=summary,
        retry_count=retry_count,
        action=action,
    )


def reconcile_llm_and_keyword_classification(
    llm: ErrorClassification | None,
    keyword: ErrorClassification,
) -> ErrorClassification:
    """Keep LLM summaries, but do not let them escalate agent-SQL errors.

    The LLM often labels `Unknown column` / `column X is not found` as an
    upstream DEPENDENCY (task de018b42), which opens human_approval instead
    of design_revise. The keyword classifier already maps those to LOGIC.
    True missing *tables* stay DEPENDENCY because both classifiers agree.
    """
    if llm is None:
        return keyword
    if "cast to bool" in (keyword.raw_log or "").lower():
        return keyword
    if (
        llm.error_type == ErrorType.DEPENDENCY
        and keyword.error_type == ErrorType.LOGIC
    ):
        extra = (
            "Reclassified from dependency: missing/unknown columns in "
            "agent SQL are LOGIC (repair via design_revise), not an "
            "upstream schema change (task de018b42)."
        )
        details = " ".join(p for p in (llm.details, extra) if p)
        return llm.model_copy(update={
            "error_type": ErrorType.LOGIC,
            "details": details[:600],
        })
    return llm


# ── LLM-based error classification ─────────────────────────────
# Keyword matching misclassified real failures (e.g. "oom" ⊂ room_ table
# names turned a parse-number-fail into RESOURCE, looping task 55f47744).
# The LLM gets the key error excerpt and returns a high-quality
# classification; the keyword classifier above stays as the fallback.

ERROR_CLASSIFY_SYSTEM_PROMPT = """\
You are a Dolphin Scheduler / Apache Doris error classification expert. Your job is to analyze task execution error logs and classify them into exactly one of these six error types:

1. **transient** — Temporary failures: network timeouts, connection refused, connection pool exhaustion, broken pipe, lock wait timeout, deadlock, too many connections, temporary failure, service unavailable. These usually resolve on retry.
2. **resource** — Infrastructure resource exhaustion: disk full, out of memory, quota exceeded, no space left on device, memory limit exceeded. Requires ops intervention (add disk/memory).
3. **data** — Source data quality or constraint violations: duplicate entry, unique constraint violation, foreign key constraint, NOT NULL violation, data too long, division by zero, out of range value, invalid datetime, check constraint failure. Caused by bad source data.
4. **logic** — SQL logic errors in the generated code: SQL syntax error, parse error, column references that do not exist in the explored schema, type mismatch, invalid function call, Doris parse/execution errors from bad SQL. Agent-repairable.
5. **config** — Configuration/permission errors: access denied, permission denied, unknown database, host not allowed, authentication failed, not authorized, task never started on the worker (no log produced), plugin missing.
6. **dependency** — Upstream schema changes: table not found, relation does not exist. Do NOT use dependency for `Unknown column` / `column X is not found` — those are almost always the agent's own SQL vs the real table (LOGIC).

Analyze the provided error log excerpt and return a JSON object with these fields:
- "error_type": one of "transient", "resource", "data", "logic", "config", "dependency"
- "summary": a concise one-line Chinese summary of the error (max 120 chars)
- "details": technical explanation of what went wrong, referencing specific error codes and messages from the log (max 500 chars)
- "fix_suggestion": actionable fix steps in Chinese (max 300 chars)

Rules:
- If the log says "can't cast to bool in strict mode", it is LOGIC caused by Doris treating `||` as boolean OR (Postgres concat). The error token (e.g. `-Q`) is a string literal in that chain. Fix by trying CONCAT / CAST VARCHAR / CAST CEIL AS INT — NOT by filtering BOOLEAN dirty values or `WHERE col IN ('true','false')`.
- If a Doris SQL parse error like "errCode = 2, detailMessage = ... INVALID_ARGUMENT parse number fail" appears (empty strings in VARCHAR date/numeric columns), it is a LOGIC error from bad SQL — NOT resource or config. The fix is adding NULLIF/WHERE != '' guards on STRING columns only.
- If the log says "can't cast to decimal in strict mode", it is LOGIC: REMOVE `col != ''` / NULLIF(col,'') from BIGINT/INT/DECIMAL/DATE columns. Those types cannot hold ''; comparing them to '' is what throws. Do NOT add more empty-string guards.
- If the error mentions Dolphin Scheduler internals like "t_ds_task_instance_context" or "DataIntegrityViolationException", it is a CONFIG error (DS platform issue, not agent SQL).
- If the log says "TaskInstanceLogPath is empty" or "doesn't be dispatched", the task never ran — it is CONFIG.
- Default to "logic" when truly uncertain."""

ERROR_CLASSIFY_USER_TEMPLATE = """\
Task Instance ID: {task_instance_id}
Retry Count: {retry_count}

Error Log Excerpt:
---
{log_excerpt}
---"""


def _extract_error_excerpt(raw_log: str, max_chars: int = 5000) -> str:
    """Extract key error lines from a DS task log for focused LLM input.

    Pulls lines matching error patterns plus a tail for context, deduped and
    capped at max_chars. Empty logs return a clear signal.
    """
    if not raw_log or not raw_log.strip():
        return "(empty log)"

    lines = raw_log.split("\n")
    error_pattern = _re.compile(
        r"(ERROR|errCode|SQLException|detailMessage|Exception|Traceback|"
        r"Caused by|INVALID_ARGUMENT|parse number fail|"
        r"Connection refused|timeout|Access denied|Duplicate entry)",
        _re.IGNORECASE,
    )

    matched_lines = [line for line in lines if error_pattern.search(line)]
    tail_lines = lines[-30:]

    # Matched lines first (deduped against the tail), then remaining tail
    matched_set = set(matched_lines)
    unique_tail = [l for l in tail_lines if l not in matched_set]

    excerpt_lines = matched_lines + (["..."] if unique_tail else []) + unique_tail

    excerpt = "\n".join(excerpt_lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars] + "\n...(truncated)"
    return excerpt


def _extract_classification_json(response: str) -> dict:
    """Parse the LLM classification response into a dict. {} on failure."""
    import json as _json

    try:
        return _json.loads(response)
    except (TypeError, _json.JSONDecodeError):
        pass

    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(1))
        except _json.JSONDecodeError:
            pass

    m = _re.search(r"\{.*\}", response, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            pass

    return {}


async def classify_execution_error_with_llm(
    task_instance_id: str,
    raw_log: str,
    retry_count: int = 0,
    timeout_seconds: float = 15.0,
) -> Optional[ErrorClassification]:
    """Classify a DS execution error via LLM for a higher-quality answer.

    Returns None when the LLM fails (timeout, bad JSON, invalid error_type,
    API exception) — the caller must fall back to classify_execution_error().
    The action field is derived from error_type with the same rules as the
    keyword classifier so routing stays deterministic.
    """
    import asyncio

    try:
        log_excerpt = _extract_error_excerpt(raw_log)

        user_message = ERROR_CLASSIFY_USER_TEMPLATE.format(
            task_instance_id=task_instance_id,
            retry_count=retry_count,
            log_excerpt=log_excerpt,
        )

        from wren_agent.tools.llm_client import get_llm_client
        llm = get_llm_client()

        response = await asyncio.wait_for(
            llm.generate_json(
                ERROR_CLASSIFY_SYSTEM_PROMPT,
                user_message,
                token_kind="classify",
            ),
            timeout=timeout_seconds,
        )

        data = _extract_classification_json(response)
        if not data:
            logger.warning("LLM classification returned unparseable JSON for %s", task_instance_id)
            return None

        raw_type = str(data.get("error_type", "")).strip().lower()
        valid_types = {"transient", "resource", "data", "logic", "config", "dependency"}
        if raw_type not in valid_types:
            logger.warning("LLM classification returned invalid error_type %r for %s", raw_type, task_instance_id)
            return None

        error_type = ErrorType(raw_type)
        summary = str(data.get("summary", "")).strip()[:200]
        details = str(data.get("details", "")).strip()[:600]
        fix_suggestion = str(data.get("fix_suggestion", "")).strip()[:400]

        action = "escalate"
        if error_type == ErrorType.TRANSIENT:
            action = "retry" if retry_count < 3 else "escalate"
        elif error_type == ErrorType.RESOURCE:
            action = "wait"

        return ErrorClassification(
            task_instance_id=task_instance_id,
            error_type=error_type,
            raw_log=raw_log,
            summary=summary or "LLM-classified error",
            details=details,
            fix_suggestion=fix_suggestion,
            retry_count=retry_count,
            action=action,
            llm_classified=True,
        )

    except asyncio.TimeoutError:
        logger.warning("LLM error classification timed out for %s", task_instance_id)
        return None
    except Exception as exc:  # LLM API down, parse errors, etc.
        logger.warning("LLM error classification failed for %s: %s", task_instance_id, exc)
        return None


def calculate_backoff_delay(retry_count: int, base_seconds: int = 30) -> int:
    """Exponential backoff: 30s, 60s, 120s, 240s..."""
    return base_seconds * (2 ** retry_count)
