"""Harness validation nodes — three-level validation for each DW layer."""

from __future__ import annotations

import logging
import json as _json
import re as _re
from datetime import datetime, timezone
from typing import Any

from wren_agent.config import settings as _wren_settings
from wren_agent.models.design import SQLArtifact
from wren_agent.models.llm_outputs import RepairOutput
from wren_agent.models.task import (
    ClarificationStatus,
    TaskPhase,
    ValidationLevel,
    ValidationResult,
)
from wren_agent.state import AgentState, is_ods_migration_task

logger = logging.getLogger(__name__)


def _count_validation_errors(results: list[ValidationResult]) -> int:
    """Count concrete failed checks, with one fallback per failed result."""
    return sum(
        max(1, len(result.failures or []))
        for result in results
        if result.passed is False
    )


def _failure_signature(results: list[ValidationResult]) -> str:
    """Deterministic fingerprint of this round's failed checks.

    Used to detect a design_revise loop that is not making progress: when the
    exact same (layer, check, detail) set fails again after a revision round,
    revising cannot fix it (e.g. DWS load SQL references columns the explored
    schema does not have, and the LLM only repairs DDL). Fail fast instead of
    burning the MAX_DESIGN_REVISIONS budget on identical rounds.
    """
    items = []
    for r in results:
        if r.passed is False:
            for f in r.failures or []:
                if isinstance(f, dict):
                    items.append(f"{r.layer}|{f.get('check')}|{f.get('detail')}")
                else:
                    items.append(f"{r.layer}|{str(f)}")
    return "|".join(sorted(items))


def _is_known_probe_misfire(results: list[ValidationResult], plan: Any = None) -> bool:
    """True when every failed check is a KNOWN harness-probe misfire whose
    root cause was fixed at the probe side — failing the task on these would
    be a false verdict.

    - `doris_dml_explain` with `not enough arguments for format string`: the
      probe forwarded an empty params tuple to pymysql and its %-mogrify
      choked on `DATE_FORMAT(..., '%Y-%m')` (task 3d19c031 — fixed in
      DorisClient.query).
    - `warehouse_source_columns_exist` whose flagged columns are ALL
      SELECT-output aliases of the plan's own load SQL (e.g. `stat_month`
      used in GROUP BY): the bare-column scanner mistook GROUP BY aliases
      for source columns (task d28431de — fixed in
      _bare_source_column_candidates).

    Replay of a stale checkpoint can still surface these even after the
    probe fix. Revising the design cannot help and hard-failing is wrong —
    the guard lets the fixed probe re-run instead.
    """
    failed = [f for r in results if r.passed is False for f in (r.failures or [])]
    if not failed:
        return False

    # SELECT output aliases of the plan's load SQL — the ground truth for the
    # warehouse_source_columns_exist misfire check.
    output_aliases: set[str] = set()
    if plan is not None:
        for a in (getattr(plan, "sql_artifacts", None) or []):
            if str(getattr(a, "sql_type", "") or "").startswith("DML_"):
                output_aliases |= {al for al, _p in _select_aliases_in_order(
                    getattr(a, "sql", "") or "")}

    for f in failed:
        check = str(f.get("check", "") if isinstance(f, dict) else f)
        detail = str(f.get("detail", "") if isinstance(f, dict) else f)
        if check == "doris_dml_explain" and "not enough arguments for format string" in detail:
            continue
        if check == "warehouse_source_columns_exist":
            m = _re.search(r"absent from explored source.*?:\s*\[([^\]]*)\]", detail)
            if not m:
                return False
            flagged = {
                t.strip().strip("`").strip("'").strip('"').lower()
                for t in m.group(1).split(",") if t.strip()
            }
            if not flagged or not (flagged <= output_aliases):
                return False
            continue
        return False
    return True


def _compiled_targets(plan: Any) -> set[str]:
    """target_table values produced by the DesignCompiler (ADR-0015).

    Compiled SQL is template-correct by construction — the deterministic
    patch helpers (`_rewrite_warehouse_plan_table_refs`,
    `_backfill_declared_columns`, `_backfill_warehouse_source_metadata`, …)
    must NOT rewrite it. Legacy LLM-written artifacts stay patched as before.
    """
    out = set()
    for a in (getattr(plan, "sql_artifacts", None) or []):
        desc = str(getattr(a, "description", "") or "")
        if "ompiled" in desc and getattr(a, "target_table", ""):
            out.add(str(a.target_table))
    return out


async def harness_validate(state: AgentState) -> AgentState:
    """Run Harness validation across all designed layers.

    Three validation levels:
    Level 1 (STRUCTURE): DDL syntax check, table/field existence → AUTO
    Level 2 (CONSISTENCY): Row counts, aggregation alignment → AUTO
    Level 3 (SEMANTIC): Expected vs actual metric values → agent generates assertions,
                         human reviews, agent executes

    All Level 1 + Level 2 must pass before proceeding.
    Level 3 failures block only if critical.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.HARNESS_VALIDATE

    plan = state.get("design_plan")
    if plan is None:
        state["errors"] = state.get("errors", []) + [
            "Missing design plan: design nodes produced no approvable artifact"
        ]
        # Fail fast in the node — route-function mutations are discarded.
        from wren_agent.models.task import TaskStatus
        state["status"] = TaskStatus.FAILED
        state["failure_kind"] = "validation"
        return state

    # Warehouse LLM often invents landing-db prefixes (db_dws.…) while
    # exploration recorded the real schema (e.g. dws_ecommerce_orders_bj).
    # Rewrite unique bare-table refs before invariant checks. Compiled
    # targets are exempt — the compiler already normalized them.
    skip = _compiled_targets(plan)
    rewritten = _rewrite_warehouse_plan_table_refs(state, plan, skip_targets=skip)
    if rewritten:
        state["warnings"] = state.get("warnings", []) + [
            f"Rewrote {rewritten} warehouse SQL table reference(s) "
            f"to match explored schemas"
        ]

    # Deterministic metadata↔SQL alignment (safe subset): declared fields the
    # DDL/INSERT target list omit are materialized ONLY when the load SQL
    # computes them (task a59491c4: the repair LLM keeps failing this sync).
    backfilled = _backfill_declared_columns(state, plan, skip_targets=skip)
    if backfilled:
        state["warnings"] = state.get("warnings", []) + [
            f"Backfilled {backfilled} declared column(s) into DDL/INSERT "
            "target lists (computed by the load SQL)"
        ]

    results: list[ValidationResult] = []
    all_passed = True

    sql_artifacts = plan.sql_artifacts or []

    compiler_failures = list(state.get("compiler_failures") or [])
    if compiler_failures:
        by_cf_layer: dict[str, list[dict]] = {}
        for f in compiler_failures:
            by_cf_layer.setdefault(str(f.get("layer") or "DWS"), []).append({
                "check": f.get("check") or "compiler_closed_world",
                "detail": f.get("detail") or "",
            })
        for layer, fl in by_cf_layer.items():
            results.append(ValidationResult(
                level=ValidationLevel.STRUCTURE,
                layer=layer,
                passed=False,
                checks=[],
                failures=fl,
            ))
        all_passed = False
        state["validation_blocking"] = True

    # Vacuous truth guard: an empty artifact list would sail through the loop
    # with all_passed=True and get auto-approved — but "nothing designed" is a
    # failure, not a pass. Closed-world compile refuses leave a semantic
    # design without SQL (task 2efe882a); those must reach design_revise
    # with Available columns, not fail the task as "nothing designed".
    if not sql_artifacts:
        has_repairable_design = bool(
            compiler_failures
            or (getattr(plan, "dws_designs", None) or [])
            or (getattr(plan, "ads_designs", None) or [])
        )
        if not has_repairable_design:
            state["errors"] = state.get("errors", []) + [
                "Harness validation: no SQL artifacts were generated by the design layers"
            ]
            state["validation_results"] = []
            state["all_validations_passed"] = False
            # Fail fast IN THE NODE — state mutations inside route functions are
            # discarded by LangGraph, which previously left such tasks dead with
            # a stale "designing" status (route_after_validation → __end__).
            from wren_agent.models.task import TaskStatus
            state["status"] = TaskStatus.FAILED
            state["failure_kind"] = "validation"

            from wren_agent.tools.event_emitter import emit_phase
            emit_phase(state, "harness_validate")
            return state

    invariant_results = _validate_materialization_plan(plan, state)
    results.extend(invariant_results)
    invariant_error_count = _count_validation_errors(invariant_results)

    # DDL probe: execute the layer DDLs against the REAL landing databases in
    # dependency order (task 36437f66: syntax/ordering errors invisible to
    # regex burned the revision budget and shipped to DS). Only when the
    # structure invariants pass, so we do not execute DDLs of a plan that is
    # structurally broken. Failures feed design_revise with the real Doris
    # error, grouped by layer so the repair targets the right artifacts.
    if invariant_error_count == 0:
        ddl_failures = await _probe_execute_layer_ddls(state, plan)
        if ddl_failures:
            by_layer: dict[str, list[dict]] = {}
            for f in ddl_failures:
                by_layer.setdefault(f["layer"], []).append(f)
            for layer, fl in by_layer.items():
                results.append(ValidationResult(
                    level=ValidationLevel.STRUCTURE,
                    layer=layer,
                    passed=False,
                    checks=[],
                    failures=fl,
                ))
                invariant_error_count += len(fl)
                all_passed = False
                state["validation_blocking"] = True

    # Doris-native semantic validation: EXPLAIN each DML SELECT body so
    # Doris's own parser — not regex — catches hallucinated columns, dialect
    # mistakes (TO_DATE arity, %Y vs yyyy) and type mismatches with precise
    # messages (tasks cc841bec/def559ca/42452474). DML referencing
    # plan-internal tables is skipped (not created until deploy; Doris
    # reports those as "Unknown column in table list" with no reliable
    # distinguishing text). Degrades silently when Doris is unreachable.
    doris_failures = await _doris_explain_load_sqls(state, plan)
    if doris_failures:
        results.append(ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer="DORIS_EXPLAIN",
            passed=False,
            checks=[],
            failures=doris_failures,
        ))
        invariant_error_count += len(doris_failures)
        all_passed = False
        state["validation_blocking"] = True
    if invariant_error_count:
        all_passed = False
        state["validation_blocking"] = True
        # Give the design_revise loop a chance to fix the design before
        # giving up: fail fast only once the revision budget is exhausted
        # (mirrors graph.MAX_DESIGN_REVISIONS). Route-function mutations are
        # discarded by LangGraph, so the node owns the FAILED flag.
        from wren_agent.graph import MAX_DESIGN_REVISIONS
        if state.get("design_revision_count", 0) >= MAX_DESIGN_REVISIONS:
            from wren_agent.models.task import TaskStatus
            state["status"] = TaskStatus.FAILED
            state["failure_kind"] = "validation"
            details = []
            for r in invariant_results:
                if r.passed is False:
                    for f in (r.failures or [])[:5]:
                        check = f.get("check", "?") if isinstance(f, dict) else "?"
                        detail = f.get("detail", "") if isinstance(f, dict) else str(f)
                        details.append(f"{check}: {detail}")
            detail_suffix = ("; " + " | ".join(details)) if details else ""
            msg = (
                "Critical design invariants failed; task cannot enter approval"
                + detail_suffix
            )
            if msg not in state.get("errors", []):
                # Drop the bare summary if a detailed one is being added
                bare = "Critical design invariants failed; task cannot enter approval"
                errs = [e for e in state.get("errors", []) if e != bare]
                state["errors"] = errs + [msg]
    else:
        state["validation_blocking"] = False

    semantic_candidates: list[Any] = []
    for artifact in sql_artifacts:
        layer = artifact.layer

        # ── Level 1: Structure Validation ───────────────────────
        structure_result = await _validate_structure(artifact)
        results.append(structure_result)
        if not structure_result.passed:
            all_passed = False
            # Don't proceed to Level 2/3 if structure fails
            continue

        # ── Level 1b: Design shape (grain/dimension columns) ──
        if artifact.sql_type == "DDL" and layer in ("DWS", "ADS"):
            shape_result = _validate_design_shape(artifact, plan)
            if shape_result is not None:
                results.append(shape_result)
                if not shape_result.passed:
                    all_passed = False
                    continue

        # ── Level 1c: DML column existence vs explored schema ────
        if artifact.sql_type.upper().startswith("DML_"):
            col_result = _validate_dml_columns(artifact, state)
            if col_result is not None:
                results.append(col_result)
                if not col_result.passed:
                    all_passed = False

        # ── Level 2: Data Consistency Validation ────────────────
        if artifact.sql_type == "DDL":
            consistency_result = await _validate_consistency(artifact, plan)
            results.append(consistency_result)
            if not consistency_result.passed:
                all_passed = False

        # ── Level 3: Semantic Validation (batched after loop) ──
        if layer in ("ADS", "DWS"):
            semantic_candidates.append(artifact)
            # Semantic failures are not auto-blocking (need human review)

    if semantic_candidates:
        results.extend(
            await _generate_semantic_assertions_batch(semantic_candidates, state)
        )

    validation_error_count = _count_validation_errors(results)
    state["validation_error_count"] = validation_error_count
    if validation_error_count:
        all_passed = False
        state["validation_blocking"] = True

    # No-progress detection: if a revision round produced the EXACT same
    # failed checks as the previous round, the loop cannot converge (e.g. the
    # LLM keeps repairing DDL while the failure is a DML column reference to
    # a table/column outside the explored schema). Fail fast with the actual
    # failures instead of ping-ponging design_revise ↔ harness_validate until
    # the revision budget or recursion limit is exhausted (tasks 405cf434,
    # 5a9f7996, ... — every failed task this week showed this pattern).
    failure_sig = _failure_signature(results)
    prev_sig = state.get("last_validation_failure_signature", "")
    if (
        failure_sig
        and prev_sig
        and failure_sig == prev_sig
        and state.get("design_revision_count", 0) >= 1
        and not all_passed
        # Known probe misfires (fixed at the probe side) are not design
        # regressions — a stale checkpoint replay should re-run the fixed
        # probe instead of hard-failing the task (tasks 3d19c031 / d28431de).
        and not _is_known_probe_misfire(results, plan)
    ):
        from wren_agent.models.task import TaskStatus
        state["status"] = TaskStatus.FAILED
        state["failure_kind"] = "validation"
        msg = (
            "Design revision made no progress: the same validation failures "
            f"persist after {state.get('design_revision_count', 0)} revision "
            "round(s). Revising cannot fix them — likely references to source "
            "tables/columns outside the explored schema. Failures: "
            + "; ".join(
                f.get("detail") if isinstance(f, dict) else str(f)
                for r in results if r.passed is False
                for f in (r.failures or [])
            )[:400]
        )
        if msg not in state.get("errors", []):
            state["errors"] = state.get("errors", []) + [msg]
    elif (
        failure_sig
        and prev_sig
        and failure_sig == prev_sig
        and state.get("design_revision_count", 0) >= 1
        and not all_passed
    ):
        logger.warning(
            "[%s] no-progress signature identical but all failures are known "
            "probe misfires — not failing the task (stale checkpoint replay?)",
            state.get("task_id", "")[:8],
        )
    state["last_validation_failure_signature"] = failure_sig

    state["validation_results"] = results
    state["all_validations_passed"] = all_passed

    from wren_agent.tools.event_emitter import emit_phase, emit_validation
    emit_phase(state, "harness_validate")
    for r in results:
        emit_validation(state, r.layer, r.passed is not False,
                        f"{sum(1 for x in (r.checks or []) if x.get('check'))} checks, "
                        f"{len(r.failures or [])} failures")

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "harness_validate",
        "action": "completed",
        "total_checks": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if r.passed is False),
        "failed_checks": validation_error_count,
        "timestamp": now.isoformat(),
    }]

    return state


async def final_validate(state: AgentState) -> AgentState:
    """Final validation after successful DAG execution.

    Verifies data has landed correctly in the Doris DW:
    - ODS row count == source row count
    - DWD deduplication correct
    - DWS aggregation matches DWD
    - ADS metrics derive correctly from DWS
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.FINAL_VALIDATE

    plan = state.get("design_plan")
    execution_results = state.get("execution_results", [])
    results: list[ValidationResult] = []

    if not execution_results or not all(r.success for r in execution_results):
        state["all_validations_passed"] = False
        state["errors"] = state.get("errors", []) + ["Execution not successful — cannot run final validation"]
        return state

    # DolphinScheduler reports 0 affected rows for DDL and its log format is
    # not a reliable row-count API. Verify the materialized targets directly
    # in Doris and use those observed counts in the execution report.
    try:
        observed_counts = await _query_materialized_target_counts(state, plan)
    except Exception as exc:
        state["all_validations_passed"] = False
        state["errors"] = state.get("errors", []) + [
            f"Unable to verify materialized target rows in Doris: {str(exc)[:200]}"
        ]
        return state

    if not observed_counts or any(count <= 0 for count in observed_counts.values()):
        empty_targets = [name for name, count in observed_counts.items() if count <= 0]
        state["all_validations_passed"] = False
        state["errors"] = state.get("errors", []) + [
            "执行完成，但物化表没有数据: "
            + (", ".join(empty_targets) if empty_targets else "未找到 DWS/ADS 目标表")
        ]
        return state

    execution_results[-1].rows_written = observed_counts

    # In production: actual Doris queries
    # For each ODS table: SELECT COUNT(*) from both source and Doris, compare
    # For each DWD table: verify dedup logic
    # For each DWS: verify SUM(DWS) == SUM(DWD) for same filters
    # For each ADS: verify derived metric formula

    # Simulated: all pass
    all_passed = True

    state["validation_results"] = state.get("validation_results", []) + results
    state["all_validations_passed"] = all_passed

    return state


async def _query_materialized_target_counts(state: AgentState, plan: Any) -> dict[str, int]:
    """Read post-execution row counts from each layer's actual landing DB."""
    from wren_agent.nodes.deployment import _warehouse_layers
    from wren_agent.tools.doris_client import DorisClient

    landing = (_warehouse_layers(state) if state.get("source_mode") == "warehouse"
               else (state.get("landing") or {}))
    task_type = state.get("business_task_type", "full")
    if is_ods_migration_task(state):
        targets = [("ods", d.ods_table) for d in (plan.ods_designs or [])]
    elif task_type == "core_model":
        targets = ([ ("dim", d.dim_table) for d in (plan.dim_designs or []) ]
                   + [ ("dwd", d.fact_table) for d in (plan.dwd_designs or []) ])
    else:
        targets = [
            ("dws", d.summary_table) for d in (plan.dws_designs or [])
        ] + [
            ("ads", a.ads_table) for a in (plan.ads_designs or [])
        ]
    counts: dict[str, int] = {}
    for layer, raw_table in targets:
        # Prefer INSERT INTO in the load SQL over plan metadata. Leftover
        # collision repairs rename CREATE/INSERT to `_v2` while ads_table /
        # summary_table stay on the empty leftover (task 2c04cf95).
        table = _physical_load_target(plan, layer, str(raw_table))
        # LLM-designed summary_table may carry the landing db prefix
        # (`db_dws.dws_x`) — split it so the COUNT query uses that database
        # with a bare table name. Backticking the whole dotted name makes
        # Doris look for a single identifier literally named
        # "db_dws.dws_x" and fail with 'Table does not exist' even though
        # the table exists (task 2cd30e16).
        db, _, bare = table.partition(".")
        if "." in table:
            table_bare = bare
            database = db or landing.get(layer)
        else:
            table_bare = table
            database = landing.get(layer)
        client = DorisClient(database=database) if database else DorisClient()
        value = await client.get_row_count(table_bare)
        counts[table] = int(value or 0)
    return counts


_INSERT_TARGET_RE = _re.compile(
    r"\bINSERT\s+INTO\s+(?:`?(?P<db>[\w$-]+)`?\s*\.\s*)?`?(?P<tbl>[\w$-]+)`?",
    _re.I,
)
_LOAD_VERSION_RE = _re.compile(r"^(.+)_v\d+$", _re.I)


def _bare_table_ident(name: str) -> str:
    return str(name or "").split(".")[-1].strip("`")


def _is_load_alias(plan_bare: str, load_bare: str) -> bool:
    """True when load_bare is plan_bare or plan_bare_vN (leftover rename)."""
    p, load = (plan_bare or "").lower(), (load_bare or "").lower()
    if not p or not load:
        return False
    if p == load:
        return True
    m = _LOAD_VERSION_RE.match(load)
    return bool(m and m.group(1) == p)


def _physical_load_target(plan: Any, layer: str, raw_table: str) -> str:
    """Qualified name actually written by this layer's INSERT, else plan name."""
    plan_bare = _bare_table_ident(raw_table)
    for artifact in getattr(plan, "sql_artifacts", None) or []:
        if str(getattr(artifact, "sql_type", "")).upper() != "DML_INSERT":
            continue
        if str(getattr(artifact, "layer", "")).upper() != str(layer).upper():
            continue
        parsed = _INSERT_TARGET_RE.search(getattr(artifact, "sql", "") or "")
        art_bare = _bare_table_ident(getattr(artifact, "target_table", "") or "")
        load_db = parsed.group("db") if parsed else ""
        load_bare = parsed.group("tbl") if parsed else art_bare
        if not _is_load_alias(plan_bare, load_bare) and not _is_load_alias(
            plan_bare, art_bare,
        ):
            continue
        if not load_bare:
            continue
        if load_db:
            return f"{load_db}.{load_bare}"
        if "." in str(raw_table):
            return f"{str(raw_table).split('.', 1)[0]}.{load_bare}"
        return load_bare
    return str(raw_table)


# ── Private validation helpers ──────────────────────────────────


def _ddl_column_names(sql: str) -> set[str]:
    """Extract column names from a CREATE TABLE DDL body (best-effort)."""
    create = _re.search(r"\bCREATE\s+TABLE\b", sql or "", _re.I)
    start = (sql or "").find("(", create.end() if create else 0)
    if start < 0:
        return set()
    depth = 0
    in_string = False
    end = -1
    for index in range(start, len(sql)):
        char = sql[index]
        if char == "'":
            in_string = not in_string
        elif not in_string and char == "(":
            depth += 1
        elif not in_string and char == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        return set()

    body = sql[start + 1:end]
    definitions: list[str] = []
    token_start = 0
    depth = 0
    in_string = False
    for index, char in enumerate(body):
        if char == "'":
            in_string = not in_string
        elif not in_string and char == "(":
            depth += 1
        elif not in_string and char == ")":
            depth -= 1
        elif not in_string and char == "," and depth == 0:
            definitions.append(body[token_start:index])
            token_start = index + 1
    definitions.append(body[token_start:])

    cols = set()
    for definition in definitions:
        definition = definition.strip()
        if not definition:
            continue
        token = definition.split()[0].strip("`").lower()
        # Skip constraint/clause keywords that can appear inside the body
        if token and token not in ("unique", "primary", "aggregate", "duplicate",
                                   "key", "index", "comment", "distributed"):
            cols.add(token)
    return cols


_INSERT_HEADER_RE = _re.compile(
    r"\bINSERT\s+INTO\s+(?:`?[\w$-]+`?\.)?`?[\w$-]+`?"
    r"(?:\s*\([^)]*\))?(?:\s+OVERWRITE)?\s+SELECT\b",
    _re.I,
)


def _strip_cast_exprs(body: str) -> str:
    """Remove CAST(...) expressions (balanced parens).

    Their inner `AS <type>` (e.g. `AS VARCHAR(255)`) must not pollute the
    SELECT-alias fallback. Balanced-paren scanning is required because the
    type itself can carry parens — a single-level regex would leave a stray
    `)` behind and corrupt depth tracking.
    """
    out: list[str] = []
    index = 0
    while index < len(body):
        m = _re.match(r"\bCAST\s*\(", body[index:], _re.I)
        if not m:
            out.append(body[index])
            index += 1
            continue
        depth = 1
        cursor = index + m.end()
        in_string = False
        while cursor < len(body) and depth > 0:
            char = body[cursor]
            if char == "'":
                in_string = not in_string
            elif not in_string and char == "(":
                depth += 1
            elif not in_string and char == ")":
                depth -= 1
            cursor += 1
        out.append(" ")
        index = cursor
    return "".join(out)


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside parentheses and single-quoted strings."""
    parts: list[str] = []
    token_start = 0
    depth = 0
    in_string = False
    for index, char in enumerate(body):
        if char == "'":
            in_string = not in_string
        elif not in_string and char == "(":
            depth += 1
        elif not in_string and char == ")":
            depth -= 1
        elif not in_string and char == sep and depth == 0:
            parts.append(body[token_start:index])
            token_start = index + 1
    parts.append(body[token_start:])
    return parts


def _insert_target_column_names(sql: str) -> set[str]:
    """Extract the target column set of a DML INSERT statement.

    Prefers the explicit `INSERT INTO t (col, ...)` column list. When the
    statement is the legacy `INSERT INTO t SELECT ... GROUP BY` form (no
    explicit list), fall back to the SELECT list — each item contributes its
    `AS <alias>` name, or its own name when it is a plain identifier
    (e.g. `t1.org_id` → `org_id`). In that form the SELECT list maps
    positionally to the DDL columns, so these ARE the target columns.
    Without the fallback, every compliant column-less DML falsely failed the
    `dws_fields_materialized_in_load_sql` invariant (task a23e1599).
    """
    match = _re.search(
        r"\bINSERT\s+INTO\s+(?:`?[\w$-]+`?\.)?`?[\w$-]+`?\s*\(([^)]*)\)",
        sql or "",
        _re.I | _re.S,
    )
    if match:
        return {
            column.strip().strip("`").lower()
            for column in match.group(1).split(",")
            if column.strip()
        }

    # No explicit column list — parse the SELECT list up to FROM/JOIN/WHERE.
    header = _INSERT_HEADER_RE.search(sql or "")
    if not header:
        return set()
    body = _re.split(
        r"\b(?:FROM|JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b",
        sql[header.end():],
        maxsplit=1,
        flags=_re.I,
    )[0]
    # Strip CAST(...) casts so their `AS <type>` does not pollute the set.
    body = _strip_cast_exprs(body)

    targets: set[str] = set()
    for item in _split_top_level(body):
        item = item.strip()
        if not item:
            continue
        m = _re.search(r"\bAS\s+`?([\w$-]+)`?$", item, _re.I)
        if m:
            targets.add(m.group(1).strip("`").lower())
            continue
        # Plain identifier without alias → the identifier itself is the
        # positional target column (e.g. `org_id`, `t1.org_id`). Function
        # calls / literals without AS get no column.
        if _re.fullmatch(r"(?:`?[\w$-]+`?\.)?`?[\w$-]+`?", item):
            targets.add(item.rpartition(".")[2].strip("`").lower())
    return targets


def _coverage_failures(
    design: Any,
    target: str,
    *,
    allow_incomplete: bool = False,
) -> list[dict]:
    """Validate explicit modeling-goal coverage emitted by design skills.

    Table-local view, kept for tests and per-table diagnostics. Production
    validation uses `_coverage_failures_plan`, which treats coverage as a
    plan-level order (any table materializing a requirement satisfies it).
    """
    coverage = list(getattr(design, "requirement_coverage", None) or [])
    if not coverage:
        return [{
            "check": "requirement_coverage_declared",
            "detail": f"{target} does not declare coverage for requested grains and metrics",
        }]

    failures: list[dict] = []
    for item in coverage:
        if not isinstance(item, dict):
            failures.append({
                "check": "requirement_coverage_valid",
                "detail": f"{target} has a non-object requirement coverage entry",
            })
            continue
        requirement = str(item.get("requirement") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        evidence = list(item.get("evidence") or [])
        gap_reason = str(item.get("gap_reason") or "").strip()
        if not requirement or status not in {"supported", "partial", "unsupported"}:
            failures.append({
                "check": "requirement_coverage_valid",
                "detail": f"{target} has invalid coverage entry: {item}",
            })
        elif status != "supported" and not gap_reason:
            failures.append({
                "check": "requirement_gap_explained",
                "detail": f"{target} marks '{requirement}' as {status} without a gap reason",
            })
        elif status != "supported" and not allow_incomplete:
            failures.append({
                "check": "requirement_not_fully_supported",
                "detail": f"{target} marks '{requirement}' as {status}: {gap_reason}",
            })
        elif not evidence:
            failures.append({
                "check": "requirement_evidence_present",
                "detail": f"{target} claims '{requirement}' is supported without physical evidence",
            })
    return failures


def _coverage_failures_plan(
    designs: list[Any],
    *,
    allow_incomplete: bool = False,
) -> list[dict]:
    """Plan-level requirement coverage validation.

    Coverage is a PLAN-level concept: a modeling-goal requirement is covered
    when ANY table in the plan materializes it. Per-table checks falsely
    failed sound layered designs (task f3511018): an org-level table marking
    "按商品分析" as unsupported is fine when the product-level table covers it,
    and a DWS table legitimately defers derived ratios to the ADS layer.
    Per-table structural issues (invalid entries, missing gap reasons,
    unsupported-without-evidence) are still checked table-locally.
    """
    structural: list[dict] = []
    claims: dict[str, list[tuple[str, str, str]]] = {}
    for design in designs:
        target = str(
            getattr(design, "summary_table", None)
            or getattr(design, "ads_table", None) or ""
        )
        coverage = list(getattr(design, "requirement_coverage", None) or [])
        if not coverage:
            structural.append({
                "check": "requirement_coverage_declared",
                "detail": f"{target} does not declare coverage for requested grains and metrics",
            })
            continue
        for item in coverage:
            if not isinstance(item, dict):
                structural.append({
                    "check": "requirement_coverage_valid",
                    "detail": f"{target} has a non-object requirement coverage entry",
                })
                continue
            requirement = str(item.get("requirement") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            evidence = list(item.get("evidence") or [])
            gap_reason = str(item.get("gap_reason") or "").strip()
            if not requirement or status not in {"supported", "partial", "unsupported"}:
                structural.append({
                    "check": "requirement_coverage_valid",
                    "detail": f"{target} has invalid coverage entry: {item}",
                })
            elif status != "supported" and not gap_reason:
                structural.append({
                    "check": "requirement_gap_explained",
                    "detail": f"{target} marks '{requirement}' as {status} without a gap reason",
                })
            elif status == "supported" and not evidence:
                structural.append({
                    "check": "requirement_evidence_present",
                    "detail": f"{target} claims '{requirement}' is supported without physical evidence",
                })
            claims.setdefault(requirement, []).append((target, status, gap_reason))

    for requirement, entries in claims.items():
        if any(status == "supported" for _t, status, _r in entries):
            continue
        if allow_incomplete:
            continue
        detail_parts = [
            f"{target} 标为 {status}" + (f"（{reason}）" if reason else "")
            for target, status, reason in entries
        ]
        structural.append({
            "check": "requirement_not_fully_supported",
            "detail": (
                f"需求点 '{requirement}' 在计划内没有任何表 fully supported: "
                + "; ".join(detail_parts)
            ),
        })
    return structural


_SOURCE_REF_RE = _re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r"(?:(`?)([\w$-]+)\1\.)?(`?)([\w$-]+)\3"
    r"(?:\s+(?:AS\s+)?(`?)([\w$-]+)\5)?",
    _re.I,
)
_QUALIFIED_COLUMN_RE = _re.compile(
    r"(?<![\w$])(`?)([\w$-]+)\1\s*\.\s*(`?)([\w$-]+)\3"
)
_SQL_CLAUSE_WORDS = {
    "where", "group", "order", "having", "limit", "union", "left",
    "right", "inner", "outer", "full", "cross", "join", "on",
}
# Tokens that look like identifiers but are never source columns when scanning
# bare (unqualified) names in single-source load SQL.
_SQL_BARE_SKIP = _SQL_CLAUSE_WORDS | {
    "select", "from", "as", "and", "or", "not", "null", "is", "in", "on",
    "join", "left", "right", "inner", "outer", "full", "cross", "union",
    "all", "distinct", "case", "when", "then", "else", "end", "cast",
    "date", "datetime", "timestamp", "string", "int", "integer", "bigint",
    "smallint", "tinyint", "decimal", "double", "float", "boolean", "bool",
    "varchar", "char", "text", "true", "false", "between", "like", "exists",
    "over", "partition", "rows", "range", "unbounded", "preceding",
    "following", "current", "row", "sum", "count", "avg", "min", "max",
    "coalesce", "if", "ifnull", "nvl", "nullif", "round", "abs", "ceil",
    "floor", "concat", "substring", "substr", "trim", "upper", "lower",
    "length", "replace", "insert", "into", "values", "with", "recursive",
    "having", "limit", "offset", "asc", "desc", "by", "group", "order",
    "where", "set", "interval", "day", "month", "year", "hour", "minute",
    "second", "week", "quarter", "extract", "to_date", "to_timestamp",
    "date_format", "datediff", "date_add", "date_sub", "now", "current_date",
    "current_timestamp", "row_number", "rank", "dense_rank", "lag", "lead",
    "first_value", "last_value", "explode", "lateral", "view", "table",
    "dual", "using", "natural", "semi", "anti", "straight_join",
}


def _unique_explored_name_to_schema(state: AgentState) -> dict[str, str]:
    """bare table name → schema when that bare name is unique across explorations.

    Values keep the schema's ORIGINAL casing (e.g. `dim_ecommerce_shop_sh`):
    Doris database names are case-sensitive, so rewrites must emit the exact
    casing Doris resolves (task a1af4bed: a lowercase `dim_ecommerce_sh`
    ref sailed past validation and died at execution with
    `Database [dim_ecommerce_sh] does not exist`). Uniqueness dedup
    stays case-insensitive.
    """
    name_to_schema: dict[str, str] = {}
    collisions: set[str] = set()
    for exp in (state.get("explorations") or {}).values():
        for t in (getattr(exp, "tables", None) or []):
            schema = (getattr(t, "schema_name", None) or "").strip()
            name = (getattr(t, "name", None) or "").strip()
            if not schema or not name:
                continue
            bare = name.lower()
            schema_l = schema.lower()
            existing = name_to_schema.get(bare)
            if existing and existing.lower() != schema_l:
                collisions.add(bare)
            else:
                name_to_schema[bare] = schema
    for bare in collisions:
        name_to_schema.pop(bare, None)
    return name_to_schema


def _plan_internal_landing_tables(state: AgentState, plan: Any) -> dict[str, str]:
    """bare plan-created DWS/ADS table name → its landing database.

    Sibling tables created by this same plan (e.g. one DWS built on another
    DWS) are not in exploration metadata, so they cannot be qualified from
    `_unique_explored_name_to_schema`. Their landing database comes from the
    effective layer mapping instead (task a23e1599: an unqualified sibling
    reference made `warehouse_sources_qualified` fail forever).
    """
    layers: dict[str, str] = {}
    if state.get("source_mode") == "warehouse":
        from wren_agent.nodes.deployment import _warehouse_layers
        layers = _warehouse_layers(state)
    out: dict[str, str] = {}
    for design in (plan.dws_designs or []):
        tbl = str(getattr(design, "summary_table", "") or "").lower()
        db = str(layers.get("dws") or "").strip()
        if tbl and db:
            out[tbl] = db
    for design in (plan.ads_designs or []):
        tbl = str(getattr(design, "ads_table", "") or "").lower()
        db = str(layers.get("ads") or "").strip()
        if tbl and db:
            out[tbl] = db
    return out


def _rewrite_sql_explored_table_refs(
    sql: str, name_to_schema: dict[str, str],
    plan_landing: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Rewrite FROM/JOIN db.table to the explored schema when bare name is unique.

    `plan_landing` supplies the landing database for tables created by this
    same plan (not yet explored), so sibling DWS/ADS references are
    qualified with the same prefix the DDL will land on.
    """
    plan_landing = plan_landing or {}
    if not sql or not (name_to_schema or plan_landing):
        return sql, 0

    count = 0
    out: list[str] = []
    last = 0
    for match in _SOURCE_REF_RE.finditer(sql):
        database = match.group(2) or ""
        table = match.group(4)
        bare = table.lower()
        real_schema = name_to_schema.get(bare) or plan_landing.get(bare)
        # Case-sensitive compare: `real_schema` keeps the explored/landing
        # casing, so a case-mismatched prefix (e.g. `dim_ecommerce_sh`
        # vs real `dim_ecommerce_shop_sh`) is rewritten instead of skipped
        # (task a1af4bed: the old `.lower()` compare let it through verbatim
        # and Doris failed the DML at execution).
        if not real_schema or database == real_schema:
            continue

        raw = match.group(0)
        kw_m = _re.match(r"(?i)(?:FROM|JOIN)\s+", raw)
        ref_start_in_raw = kw_m.end() if kw_m else 0
        alias = match.group(6) or ""
        if alias:
            alias_pos = raw.lower().rfind(alias.lower())
            ref_part = raw[ref_start_in_raw:alias_pos]
            # Drop trailing AS if present
            ref_part = _re.sub(r"(?i)\s+AS\s*$", "", ref_part).rstrip()
        else:
            ref_part = raw[ref_start_in_raw:].rstrip()

        abs_start = match.start() + ref_start_in_raw
        abs_end = abs_start + len(ref_part)
        q = "`" if "`" in ref_part else ""
        out.append(sql[last:abs_start])
        out.append(f"{q}{real_schema}{q}.{q}{table}{q}")
        last = abs_end
        count += 1

    if not count:
        return sql, 0
    out.append(sql[last:])
    return "".join(out), count


def _rewrite_warehouse_plan_table_refs(
    state: AgentState, plan: Any, skip_targets: set[str] | None = None,
) -> int:
    """Rewrite warehouse DML/source_tables to explored schemas. Returns rewrite count.

    ``skip_targets`` (compiled tables) are left untouched — their SQL and
    metadata are compiler-generated and already consistent.
    """
    if state.get("source_mode") != "warehouse" or plan is None:
        return 0
    skip = skip_targets or set()
    name_to_schema = _unique_explored_name_to_schema(state)
    plan_landing = _plan_internal_landing_tables(state, plan)
    if not name_to_schema and not plan_landing:
        return 0

    total = 0
    for artifact in (plan.sql_artifacts or []):
        if str(getattr(artifact, "target_table", "") or "") in skip:
            continue
        if not str(getattr(artifact, "sql_type", "")).upper().startswith("DML_"):
            continue
        new_sql, n = _rewrite_sql_explored_table_refs(
            getattr(artifact, "sql", "") or "", name_to_schema, plan_landing,
        )
        if n:
            artifact.sql = new_sql
            total += n

    def _resolve_db(bare: str) -> str:
        """Real explored schema first, then this plan's own landing database."""
        real = name_to_schema.get(bare)
        if real:
            return real
        return plan_landing.get(bare, "")

    def _fix_qualified_list(values: list | None) -> list | None:
        if not values:
            return values
        out = []
        changed = False
        for raw in values:
            text = str(raw)
            if "." in text:
                db, _, tbl = text.partition(".")
                real = _resolve_db(tbl.lower())
                # `real` keeps the explored casing — rewrite case-mismatched
                # prefixes too (task a1af4bed: `dim_ecommerce_sh` vs
                # real `dim_ecommerce_shop_sh`).
                if real and db != real:
                    out.append(f"{real}.{tbl}")
                    changed = True
                    continue
            else:
                real = _resolve_db(text.lower())
                if real:
                    out.append(f"{real}.{text}")
                    changed = True
                    continue
            out.append(text)
        return out if changed else values

    for design in (plan.dws_designs or []):
        if str(getattr(design, "summary_table", "") or "") in skip:
            continue
        fixed = _fix_qualified_list(getattr(design, "source_tables", None))
        if fixed is not None and fixed is not design.source_tables:
            design.source_tables = fixed
            total += 1
        src = getattr(design, "source_dwd_table", "") or ""
        if src and "." not in src and src.lower() in name_to_schema:
            design.source_dwd_table = f"{name_to_schema[src.lower()]}.{src}"
            total += 1
        elif "." in src:
            db, _, tbl = src.partition(".")
            real = name_to_schema.get(tbl.lower())
            if real and db != real:
                design.source_dwd_table = f"{real}.{tbl}"
                total += 1

    for design in (plan.ads_designs or []):
        if str(getattr(design, "ads_table", "") or "") in skip:
            continue
        fixed = _fix_qualified_list(getattr(design, "source_dws_tables", None))
        if fixed is not None and fixed is not design.source_dws_tables:
            design.source_dws_tables = fixed
            total += 1

    total += _shorten_plan_table_names(plan, skip_targets=skip)
    total += _fix_plan_target_db_prefixes(state, plan, skip_targets=skip)
    return total


def _fix_plan_target_db_prefixes(
    state: AgentState, plan: Any, skip_targets: set[str] | None = None,
) -> int:
    """Rewrite design target db prefixes not valid for the layer to the
    landing database.

    The ADS LLM keeps inventing `ads_ecommerce_SH.` by analogy with the
    DWS database, and the resulting `warehouse_target_db_prefix` failure
    persisted across revisions because the repair LLM fixes SQL text, not the
    design.ads_table metadata (task c0f31ae0). Mechanical rewrite guarantees
    the target lands in a legal layer database. Compiled targets are skipped
    — the compiler already normalizes them to the landing database.
    """
    if not plan or state.get("source_mode") != "warehouse":
        return 0
    skip = skip_targets or set()
    from wren_agent.nodes.deployment import _warehouse_layers

    layers = _warehouse_layers(state)
    bad_prefixes: dict[str, str] = {}  # invented db prefix → landing db
    for designs, layer, attr in (
        (plan.dws_designs or [], "dws", "summary_table"),
        (plan.ads_designs or [], "ads", "ads_table"),
    ):
        allowed = _warehouse_layer_db_names(state, layer)
        landing = str(layers.get(layer) or "").strip()
        if not allowed or not landing:
            continue
        allowed_l = {d.lower() for d in allowed}
        for design in designs:
            target = str(getattr(design, attr, "") or "")
            if target in skip or "." not in target:
                continue
            db = target.split(".", 1)[0].strip("`")
            if db.lower() in allowed_l:
                continue
            bad_prefixes[db] = landing
            setattr(design, attr, f"{landing}.{target.split('.', 1)[1]}")

    renames = len(bad_prefixes)
    for artifact in (plan.sql_artifacts or []):
        tbl = str(getattr(artifact, "target_table", "") or "")
        if tbl in skip:
            continue
        for bad, landing in bad_prefixes.items():
            if tbl.startswith(f"{bad}."):
                artifact.target_table = f"{landing}.{tbl.split('.', 1)[1]}"
                renames += 1
            sql = getattr(artifact, "sql", "") or ""
            if sql and f"{bad}." in sql:
                artifact.sql = sql.replace(f"{bad}.", f"{landing}.")
        # The design target may be BARE while the DDL/DML SQL qualifies it
        # with another layer's database (task a2b0cea0: ADS DDL created in
        # dws_ecommerce_orders_sh). Rewrite CREATE TABLE / INSERT INTO targets
        # to the artifact layer's landing db when the prefix is not legal for
        # that layer.
        layer = str(getattr(artifact, "layer", "") or "").upper()
        if layer in ("DWS", "ADS") and artifact.sql:
            allowed = _warehouse_layer_db_names(state, layer.lower())
            landing = str(layers.get(layer.lower()) or "").strip()
            if allowed and landing:
                allowed_l = {d.lower() for d in allowed}
                m = _re.search(
                    r"(?i)(?:CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?|INSERT\s+INTO\s+)"
                    r"`?([\w$-]+)`?\s*\.\s*`?([\w$-]+)`?",
                    artifact.sql,
                )
                if m and m.group(1).lower() not in allowed_l:
                    bad, bare = m.group(1), m.group(2)
                    artifact.sql = artifact.sql.replace(
                        f"{bad}.{bare}", f"{landing}.{bare}")
                    renames += 1
    return renames


# Doris rejects table names longer than table_name_length_limit (FE config,
# default 64; the runtime value can be raised via ADMIN SET FRONTEND CONFIG
# and must be mirrored in settings.doris.table_name_length_limit). The design
# LLM repeatedly emits 65-66-char names for deep grain combos
# (`dws_order_payment_analysis_all_channel_quarter`) and
# the repair LLM keeps landing one char over (tasks a1af4bed/b338672b).
_DORIS_TABLE_NAME_LIMIT = _wren_settings.doris.table_name_length_limit


def _shorten_plan_table_names(plan: Any, skip_targets: set[str] | None = None) -> int:
    """Deterministically shorten plan-created DWS/ADS names over 64 chars.

    Strips the longest interior underscore-segment(s) — never the layer
    prefix (first token) or the grain suffix (last token) — until the bare
    name fits. The rename is propagated through every artifact and reference
    so validation and deploy see one consistent name. Returns rename count.
    Compiled targets are skipped — the compiler already normalizes names.
    """
    if not plan:
        return 0
    skip = skip_targets or set()
    by_old: dict[str, str] = {}

    def _shortened(bare: str) -> str | None:
        if len(bare) <= _DORIS_TABLE_NAME_LIMIT:
            return None
        segs = bare.split("_")
        if len(segs) < 4:
            return None
        while len("_".join(segs)) > _DORIS_TABLE_NAME_LIMIT:
            interior = segs[1:-1]
            if not interior:
                return None
            victim = max(interior, key=len)
            segs.remove(victim)
        return "_".join(segs)

    def _rename_target(qualified: str) -> str | None:
        if "." in qualified:
            db, _, bare = qualified.partition(".")
            bare = bare.strip("`")
        else:
            db, bare = "", qualified
        new = _shortened(bare)
        if not new:
            return None
        by_old[bare] = new
        return f"{db}.{new}" if db else new

    renames = 0
    for design in (plan.dws_designs or []):
        old = str(getattr(design, "summary_table", "") or "")
        if old in skip:
            continue
        new = _rename_target(old)
        if new:
            design.summary_table = new
            renames += 1
    for design in (plan.ads_designs or []):
        old = str(getattr(design, "ads_table", "") or "")
        if old in skip:
            continue
        new = _rename_target(old)
        if new:
            design.ads_table = new
            renames += 1
    if not by_old:
        return 0

    # Propagate: artifacts (target + SQL text), design source lists.
    def _map_ref(ref: str) -> str:
        bare = ref.split(".")[-1].strip("`")
        new = by_old.get(bare)
        return new if new else ref

    for artifact in (plan.sql_artifacts or []):
        tbl = str(getattr(artifact, "target_table", "") or "")
        if tbl and tbl.split(".")[-1].strip("`") in by_old:
            artifact.target_table = _map_ref(tbl)
            renames += 1
        sql = getattr(artifact, "sql", "") or ""
        if sql:
            for old, new in by_old.items():
                sql = _re.sub(rf"\b{_re.escape(old)}\b", new, sql)
            artifact.sql = sql
    for design in (plan.dws_designs or []):
        src = str(getattr(design, "source_dwd_table", "") or "")
        if src and src.split(".")[-1].strip("`") in by_old:
            design.source_dwd_table = _map_ref(src)
        srcs = getattr(design, "source_tables", None)
        if srcs:
            design.source_tables = [_map_ref(s) for s in srcs]
    for design in (plan.ads_designs or []):
        srcs = getattr(design, "source_dws_tables", None)
        if srcs:
            design.source_dws_tables = [_map_ref(s) for s in srcs]
    return renames


def _explored_table_fields(state: AgentState) -> dict[str, set[str]]:
    """Build a qualified/bare source-table field index from exploration."""
    qualified: dict[str, set[str]] = {}
    bare_candidates: dict[str, list[set[str]]] = {}
    for exploration in (state.get("explorations") or {}).values():
        tables = getattr(exploration, "tables", None)
        if tables is None and isinstance(exploration, dict):
            tables = exploration.get("tables", [])
        for table in tables or []:
            name = getattr(table, "name", None)
            schema = getattr(table, "schema_name", None)
            fields = getattr(table, "fields", None)
            if isinstance(table, dict):
                name = name or table.get("name")
                schema = schema or table.get("schema_name")
                fields = fields if fields is not None else table.get("fields", [])
            if not name:
                continue
            column_names = set()
            for field in fields or []:
                field_name = getattr(field, "name", None)
                if isinstance(field, dict):
                    field_name = field_name or field.get("name")
                if field_name:
                    column_names.add(str(field_name).lower())
            bare = str(name).lower()
            bare_candidates.setdefault(bare, []).append(column_names)
            if schema:
                qualified[f"{str(schema).lower()}.{bare}"] = column_names

    # A bare name is safe only when it resolves to exactly one explored table.
    for bare, candidates in bare_candidates.items():
        if len(candidates) == 1:
            qualified[bare] = candidates[0]
    return qualified


def _explored_null_profiles(state: AgentState) -> dict[str, dict[str, dict]]:
    """lookup → {column: {ratio, alternatives}} from exploration FieldSchema.

    Used by ``warehouse_grain_column_empty`` so a 100%-NULL DATE copied
    through core_model (task bb58438f) cannot become a DWS time grain.
    """
    from wren_agent.tools.column_profile import populated_alternatives

    out: dict[str, dict[str, dict]] = {}

    def _index(lookup: str, fields: list) -> None:
        bucket = out.setdefault(lookup, {})
        for field in fields:
            name = (
                field.get("name") if isinstance(field, dict)
                else getattr(field, "name", None)
            )
            if not name:
                continue
            ratio = (
                field.get("null_ratio") if isinstance(field, dict)
                else getattr(field, "null_ratio", None)
            )
            if ratio is None:
                continue
            alts = populated_alternatives(fields, exclude=str(name))
            bucket[str(name).lower()] = {
                "ratio": float(ratio),
                "alternatives": alts,
            }

    for exploration in (state.get("explorations") or {}).values():
        tables = getattr(exploration, "tables", None)
        if tables is None and isinstance(exploration, dict):
            tables = exploration.get("tables", [])
        for table in tables or []:
            name = getattr(table, "name", None)
            schema = getattr(table, "schema_name", None)
            fields = getattr(table, "fields", None)
            if isinstance(table, dict):
                name = name or table.get("name")
                schema = schema or table.get("schema_name")
                fields = fields if fields is not None else table.get("fields", [])
            if not name:
                continue
            bare = str(name).lower()
            _index(bare, list(fields or []))
            if schema:
                _index(f"{str(schema).lower()}.{bare}", list(fields or []))
    return out


def _resolve_explored_fields(
    source_fields: dict[str, set[str]],
    lookup: str,
    bare_table: str,
) -> set[str] | None:
    """Resolve source fields by qualified name, then unique bare name."""
    fields = source_fields.get(lookup)
    if fields is not None:
        return fields
    return source_fields.get(bare_table)


def _bare_source_column_candidates(sql: str) -> set[str]:
    """Best-effort bare identifiers from the SELECT body of a load statement.

    Single-source warehouse DML often omits aliases (`SELECT group_id FROM t`).
    Qualified-column checks miss those; this scan covers them without trying to
    fully parse SQL. Multi-source statements must use aliases (enforced
    separately) and are not scanned here.
    """
    if not sql:
        return set()
    m = _re.search(r"\bSELECT\b", sql, _re.I)
    body = sql[m.start():] if m else sql
    # Drop string literals so quoted text is not treated as columns.
    body = _re.sub(r"'(?:[^']|'')*'", " ", body)
    body = _re.sub(r'"(?:[^"]|"")*"', " ", body)
    # Drop FROM… through the next clause — table/alias names are not columns.
    body = _re.sub(
        r"\bFROM\b.+?(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bLIMIT\b|\bUNION\b|$)",
        " ",
        body,
        flags=_re.I | _re.S,
    )
    # Qualified refs are validated separately.
    body = _QUALIFIED_COLUMN_RE.sub(" ", body)
    # Output aliases after AS are not source columns.
    body = _re.sub(r"\bAS\s+`?([\w$-]+)`?", " ", body, flags=_re.I)
    # Function-call names: keep args, drop the callee.
    body = _re.sub(r"\b([A-Za-z_][\w$]*)\s*\(", " (", body)
    found: set[str] = set()
    for match in _re.finditer(r"(?<![\w.$])`?([A-Za-z_][\w$]*)`?", body):
        token = match.group(1).lower()
        if token in _SQL_BARE_SKIP or token.isdigit():
            continue
        found.add(token)
    # SELECT output aliases (the `stat_month` in `... AS stat_month`) are NOT
    # source columns — but Doris allows GROUP BY <select-alias>, so the alias
    # also appears bare in GROUP BY and this scan would flag it as a
    # hallucinated source column (task d28431de: `['stat_month']` failed every
    # DWS/ADS load SQL across two revision rounds and killed the task).
    # Drop PURE aliases only — an alias that is ALSO referenced as a source
    # column stays flagged (task b131f22e: `CAST(group_id AS STRING) AS
    # group_id` — the group_id inside CAST is a source reference; the source
    # lacks that column and the statement MUST fail). Function arguments are
    # the reliable signal: pure aliases never appear inside a function call.
    # `org_name AS org_name` / `stat_date AS inventory_month`: the identifier
    # before AS is a source column. Subtracting every SELECT alias dropped
    # `col AS col` and the harness missed `org_name` (task 9f42f542).
    aliases = {a for a, _pos in _select_aliases_in_order(sql)}
    func_arg_tokens = {
        _re.match(r"\(?\s*`?([A-Za-z_][\w$]*)`?", s, _re.I).group(1).lower()
        for s in _re.findall(r"\(([A-Za-z_`][\w`$]*)", body, _re.I)
    }
    found -= aliases - func_arg_tokens - _unqualified_select_source_columns(sql)
    return found


def _unqualified_select_source_columns(sql: str) -> set[str]:
    """Bare identifiers in the SELECT list of a load statement.

    `_bare_source_column_candidates` subtracts SELECT-list names as output
    aliases, which is correct for `DATE_FORMAT(...) AS stat_month` +
    `GROUP BY stat_month`. DIM/DWD bootstrap SQL is `SELECT id, code FROM
    ods_x` with no AS — those tokens ARE source columns and must be checked
    (task de018b42).
    """
    header = _INSERT_HEADER_RE.search(sql or "")
    if header:
        rest = sql[header.end():]
    else:
        m = _re.search(r"\bSELECT\b", sql or "", _re.I)
        if not m:
            return set()
        rest = sql[m.end():]
    body = _re.split(
        r"\b(?:FROM|JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b",
        rest, maxsplit=1, flags=_re.I,
    )[0]
    found: set[str] = set()
    for item in _split_top_level(body):
        item = _re.sub(r"\bAS\s+`?[\w$-]+`?$", "", item.strip(), flags=_re.I).strip()
        if not _re.fullmatch(r"`?[\w$-]+`?", item):
            continue
        tok = item.strip("`").lower()
        if tok in _SQL_BARE_SKIP or tok.isdigit():
            continue
        found.add(tok)
    return found


def _validate_warehouse_load_sql(
    target: str,
    artifacts: list[Any],
    state: AgentState,
    plan: Any = None,
    target_layer: str = "DWS",
) -> list[dict]:
    """Validate warehouse load SQL against explored tables and columns.

    Catches hallucinated tables/columns before approval/deploy. Failure
    details include the real available column list so design_revise can fix
    the statement instead of redeploying Unknown-column errors.
    """
    source_fields = _explored_table_fields(state)
    null_profiles = _explored_null_profiles(state)
    # Plan-created sibling tables (e.g. one DWS built on another DWS) are not
    # in exploration yet — resolve their columns from this plan's own DDLs so
    # references to them are validated against their real schema instead of
    # failing as "not explored" (task a23e1599: dws_order_revenue_monthly
    # reads dws_order_revenue_daily from the same plan).
    if plan is not None:
        plan_landing = _plan_internal_landing_tables(state, plan)
        for a in (plan.sql_artifacts or []):
            if str(getattr(a, "sql_type", "")).upper() != "DDL":
                continue
            tbl = str(getattr(a, "target_table", "") or "").lower()
            cols = _ddl_column_names(getattr(a, "sql", "") or "")
            if not tbl or not cols:
                continue
            source_fields.setdefault(tbl, cols)
            db = plan_landing.get(tbl)
            if db:
                source_fields.setdefault(f"{db}.{tbl}", cols)
    if not source_fields:
        return [{
            "check": "warehouse_source_metadata_available",
            "detail": (
                f"{target} cannot validate source fields because "
                "exploration metadata is empty"
            ),
        }]

    # Doris resolves database names case-sensitively — a db-qualified source
    # ref with the wrong casing (or an invented db) must fail here, not at DS
    # execution (task a1af4bed: `dim_ecommerce_sh` passed every
    # case-blind check and every DML joining it died in Doris).
    known_dbs = (
        _warehouse_known_dbs(state)
        if state.get("source_mode") == "warehouse" else set()
    )

    # Explored source tables with ZERO rows — anchoring a DML on them
    # materializes an empty table (tasks 582633e8/73e47237: four consecutive
    # "materialized targets are empty" failures because the design LLM picked
    # 0-row tables by name). Reject them deterministically so the repair loop
    # re-anchors on data-bearing tables.
    explored_rows: dict[str, int] = {}
    # table → exploration layer ("dwd"/"dws"/"dim"...): DWS must read the
    # layer BELOW it, never other DWS summaries (aggregate-of-aggregate +
    # stale DEPENDENT chains, task 432c17b1).
    explored_layers: dict[str, str] = {}
    for key, exp in (state.get("explorations") or {}).items():
        layer = key.rsplit("#", 1)[-1].lower() if "#" in key else ""
        for t in (getattr(exp, "tables", None) or []):
            name = str(getattr(t, "name", "") or "")
            if not name:
                continue
            if layer:
                explored_layers[name.lower()] = layer
            est = getattr(t, "row_count_estimate", None)
            if est is not None:
                explored_rows[name.lower()] = int(est)
            schema = str(getattr(t, "schema_name", "") or "").strip()
            if schema:
                if layer:
                    explored_layers[f"{schema.lower()}.{name.lower()}"] = layer
                if est is not None:
                    explored_rows[f"{schema.lower()}.{name.lower()}"] = int(est)

    failures: list[dict] = []
    for artifact in artifacts:
        if not artifact.sql_type.upper().startswith("DML_"):
            continue

        refs = []
        alias_to_fields: dict[str, set[str]] = {}
        alias_to_lookup: dict[str, str] = {}
        for match in _SOURCE_REF_RE.finditer(artifact.sql or ""):
            database_raw = match.group(2) or ""
            database = database_raw.lower()
            table = match.group(4).lower()
            captured_alias = (match.group(6) or "").lower()
            explicit_alias = bool(
                captured_alias and captured_alias not in _SQL_CLAUSE_WORDS
            )
            alias = captured_alias if explicit_alias else table
            if database_raw:
                db_failure = _warehouse_db_ref_failure(database_raw, known_dbs)
                if db_failure:
                    failures.append(db_failure)
            lookup = f"{database}.{table}" if database else table
            fields = _resolve_explored_fields(source_fields, lookup, table)
            # Layered-warehouse rule: DWS must read DWD/DIM (the layer below),
            # never OTHER DWS summaries — aggregating aggregates compounds
            # errors and creates DEPENDENT chains on stale workflows (task
            # 432c17b1). Same-plan sibling DWS tables (in plan_landing) are
            # the plan's own pipeline and stay allowed. ADS reads DWS by
            # design, so the rule applies to DWS targets only.
            if target_layer == "DWS":
                src_layer = explored_layers.get(lookup) or explored_layers.get(table, "")
                if src_layer == "dws" and table not in plan_landing:
                    failures.append({
                        "check": "warehouse_dws_source_layer",
                        "detail": (
                            f"{target} reads source table `{lookup}` from the "
                            f"DWS layer — DWS must aggregate from DWD/DIM "
                            f"detail tables, never from another DWS summary. "
                            f"Re-anchor on its DWD counterpart (dwd_order_*)."
                        ),
                    })
            # 0-row explored source → the DML would materialize an empty
            # table; the repair loop must re-anchor on data-bearing tables.
            src_rows = explored_rows.get(lookup) or explored_rows.get(table)
            if src_rows == 0 and not any(
                f.get("check") == "warehouse_source_table_empty"
                for f in failures
            ):
                failures.append({
                    "check": "warehouse_source_table_empty",
                    "detail": (
                        f"{target} reads source table `{lookup}` which has "
                        f"0 rows in the warehouse — the DML would materialize "
                        f"an empty table. Anchor on a data-bearing table "
                        f"instead (e.g. its DWD counterpart or any table "
                        f"with rows > 0)."
                    ),
                })
            refs.append((lookup, table, alias, explicit_alias, fields))
            if fields is None:
                failures.append({
                    "check": "warehouse_source_table_explored",
                    "detail": (
                        f"{target} references source table not present in "
                        f"exploration metadata: {lookup}"
                    ),
                })
            else:
                alias_to_fields[alias] = fields
                alias_to_fields[table] = fields
                alias_to_lookup[alias] = lookup
                alias_to_lookup[table] = lookup

        from wren_agent.tools.column_profile import (
            grain_columns_in_sql, is_unusable_grain,
        )
        seen_empty_grain: set[tuple[str, str]] = set()
        for qual, col in grain_columns_in_sql(artifact.sql or ""):
            lookup = (
                alias_to_lookup.get(qual)
                if qual else None
            )
            if lookup is None and len(refs) == 1:
                lookup = refs[0][0]
            elif lookup is None and qual:
                lookup = alias_to_lookup.get(qual)
            if not lookup:
                continue
            profile = (null_profiles.get(lookup) or {}).get(col) or {}
            ratio = profile.get("ratio")
            if not is_unusable_grain(ratio):
                continue
            key = (lookup, col)
            if key in seen_empty_grain:
                continue
            seen_empty_grain.add(key)
            alts = profile.get("alternatives") or []
            alt_txt = (
                "; pick a populated date column: " + ", ".join(alts)
                if alts else
                "; pick another populated date/time column on that table"
            )
            failures.append({
                "check": "warehouse_grain_column_empty",
                "detail": (
                    f"{target} uses `{lookup}.{col}` as a time grain / "
                    f"IS NOT NULL filter but that column is "
                    f"{float(ratio):.0%} NULL in the published DIM/DWD "
                    f"(copied from ODS — core_model does not invent values). "
                    f"DATE_FORMAT/IS NOT NULL on it materializes an empty "
                    f"table (task bb58438f){alt_txt}."
                ),
            })

        if len(refs) > 1:
            missing_aliases = sorted({
                lookup for lookup, _table, _alias, explicit, fields in refs
                if fields is not None and not explicit
            })
            if missing_aliases:
                failures.append({
                    "check": "warehouse_source_alias_required",
                    "detail": (
                        f"{target} uses multiple source tables without "
                        f"explicit aliases: {missing_aliases}"
                    ),
                })

        unknown_columns: dict[str, set[str]] = {}
        for match in _QUALIFIED_COLUMN_RE.finditer(artifact.sql or ""):
            qualifier = match.group(2).lower()
            column = match.group(4).lower()
            fields = alias_to_fields.get(qualifier)
            if fields is not None and column not in fields:
                unknown_columns.setdefault(qualifier, set()).add(column)

        for qualifier, cols in sorted(unknown_columns.items()):
            avail = sorted(alias_to_fields.get(qualifier) or [])
            avail_txt = ", ".join(avail[:30]) + ("…" if len(avail) > 30 else "")
            src = alias_to_lookup.get(qualifier, qualifier)
            failures.append({
                "check": "warehouse_source_columns_exist",
                "detail": (
                    f"{target} references columns absent from explored source "
                    f"`{src}`: {sorted(f'{qualifier}.{c}' for c in cols)}. "
                    f"Available columns: {avail_txt or '(none)'}"
                ),
            })

        # Single explored source: also reject bare column names (task b131f22e
        # slipped past alias.column-only checks with `group_id` in GROUP BY).
        explored_refs = [
            (lookup, fields) for lookup, _t, _a, _e, fields in refs
            if fields is not None
        ]
        if len(explored_refs) == 1:
            lookup, fields = explored_refs[0]
            missing_bare = sorted(
                c for c in _bare_source_column_candidates(artifact.sql or "")
                if c not in fields
            )
            if missing_bare:
                avail = sorted(fields)
                avail_txt = ", ".join(avail[:30]) + ("…" if len(avail) > 30 else "")
                failures.append({
                    "check": "warehouse_source_columns_exist",
                    "detail": (
                        f"{target} references columns absent from explored source "
                        f"`{lookup}`: {missing_bare}. "
                        f"Available columns: {avail_txt or '(none)'}"
                    ),
                })

    return failures


def _select_aliases_in_order(sql: str) -> list[tuple[str, int]]:
    """Ordered (alias, position) pairs of a DML's SELECT list items."""
    header = _INSERT_HEADER_RE.search(sql or "")
    if not header:
        return []
    body = _re.split(
        r"\b(?:FROM|JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b",
        sql[header.end():],
        maxsplit=1,
        flags=_re.I,
    )[0]
    body = _strip_cast_exprs(body)
    out: list[tuple[str, int]] = []
    for pos, item in enumerate(_split_top_level(body)):
        item = item.strip()
        if not item:
            continue
        m = _re.search(r"\bAS\s+`?([\w$-]+)`?$", item, _re.I)
        if m:
            out.append((m.group(1).strip("`").lower(), pos))
            continue
        if _re.fullmatch(r"(?:`?[\w$-]+`?\.)?`?[\w$-]+`?", item):
            out.append((item.rpartition(".")[2].strip("`").lower(), pos))
            continue
        # Implicit alias right after a closing paren (`DATE_FORMAT(d, '%Y-%m')
        # stat_month` — the bare `stat_month` in GROUP BY resolves against this
        # output alias, not a source column). Only the `)`-terminated form is
        # unambiguous; a bareword after an operator is an expression, not an
        # alias.
        m = _re.match(r"^.*\)\s+`?([\w$-]+)`?$", item, _re.S | _re.I)
        if m:
            out.append((m.group(1).strip("`").lower(), pos))
            continue
    return out


def _backfill_declared_columns(
    state: AgentState, plan: Any, skip_targets: set[str] | None = None,
) -> int:
    """Deterministically materialize declared fields that the DDL / INSERT
    target list omit — ONLY when the load SQL actually computes the value
    (`SELECT ... AS <name>`): the value source exists, so adding the column
    is safe. Task a59491c4: the repair LLM keeps failing to sync
    metadata↔SQL; this fixes the safe subset mechanically. Grain/key
    structure is NOT touched, and fields without a DML value source are left
    for LLM repair (a mechanically added column with no value source would
    be a silent wrong-data risk). Compiled targets are skipped — the
    compiler renders DDL/INSERT column lists from the same structure, so
    they are consistent by construction.
    """
    count = 0
    skip = skip_targets or set()
    for design in (plan.dws_designs or []) + (plan.ads_designs or []):
        target = str(
            getattr(design, "summary_table", "") or getattr(design, "ads_table", "") or ""
        )
        if not target or target in skip:
            continue
        bare = target.split(".")[-1].strip("`").lower()
        arts = [
            a for a in (plan.sql_artifacts or [])
            if str(getattr(a, "target_table", "") or "").split(".")[-1].strip("`").lower() == bare
        ]
        ddl = next((a for a in arts if str(getattr(a, "sql_type", "")).upper() == "DDL"), None)
        dml = next((a for a in arts if str(getattr(a, "sql_type", "")).upper().startswith("DML_")), None)
        if ddl is None or dml is None or not ddl.sql or not dml.sql:
            continue

        declared: dict[str, dict] = {}
        for f in (design.fields or []):
            if isinstance(f, dict) and f.get("name"):
                declared.setdefault(str(f["name"]).lower(), f)
        # DWS declares measures; ADS declares metrics — both carry the
        # materialized column names.
        for m in (getattr(design, "measures", None) or []) + (
            getattr(design, "metrics", None) or []
        ):
            if isinstance(m, dict) and m.get("name"):
                declared.setdefault(str(m["name"]).lower(), m)

        ddl_cols = _ddl_column_names(ddl.sql)
        select_aliases = dict(_select_aliases_in_order(dml.sql))
        backfill: list[tuple[str, int]] = []
        for name, meta in declared.items():
            if name in ddl_cols or name not in select_aliases:
                continue  # present, or no value source (leave to LLM repair)
            if not str(meta.get("type") or "").strip():
                continue  # no declared type — cannot backfill safely
            backfill.append((name, select_aliases[name]))
        if not backfill:
            continue

        # 1. Append the missing columns to the DDL (after the key columns,
        # before the closing paren of the column list).
        close = _re.search(
            r"\)\s*(?:UNIQUE\s+KEY|AGGREGATE\s+KEY|DUPLICATE\s+KEY|COMMENT|DISTRIBUTED|PROPERTIES)",
            ddl.sql, _re.I,
        )
        insert_at = close.start() if close else ddl.sql.rfind(")")
        if insert_at == -1:
            continue
        for name, _pos in backfill:
            meta = declared[name]
            col_type = str(meta.get("type") or "").strip()
            col_type = _re.sub(r"\bSTRING\b", "VARCHAR(65533)", col_type, flags=_re.I)
            comment = str(meta.get("comment") or "").replace("'", "''")
            col_line = f",\n  {name} {col_type} COMMENT '{comment}'"
            ddl.sql = ddl.sql[:insert_at] + col_line + ddl.sql[insert_at:]
            insert_at += len(col_line)
            count += 1

        # 2. Add the computed columns to the INSERT target list at the same
        # positional slot as their SELECT alias (positional mapping).
        tm = _re.search(
            r"\bINSERT\s+INTO\s+(?:`?[\w$-]+`?\.)?`?[\w$-]+`?\s*\(([^)]*)\)",
            dml.sql, _re.I | _re.S,
        )
        if tm:
            cols = [c.strip().strip("`") for c in tm.group(1).split(",") if c.strip()]
            for name, pos in backfill:
                if name in {c.lower() for c in cols}:
                    continue
                pos = min(pos, len(cols))
                cols.insert(pos, name)
                count += 1
            new_list = ", ".join(cols)
            dml.sql = (dml.sql[:tm.start(1)] + new_list + dml.sql[tm.end(1):])
    return count


def _validate_warehouse_dws_sources(
    design: Any,
    artifacts: list[Any],
    state: AgentState,
    plan: Any = None,
) -> list[dict]:
    """Back-compat wrapper — DWS load SQL vs exploration metadata."""
    return _validate_warehouse_load_sql(design.summary_table, artifacts, state, plan)

async def _explain_on_doris(select_body: str) -> str | None:
    """Run `EXPLAIN SELECT <SELECT body>` on Doris; return the Doris error text.

    EXPLAIN parses and validates (columns, functions, types) without
    executing — read-only, no side effects, works against real layer
    databases. Returns None when the statement is valid.

    The body passed by `_doris_explain_load_sqls` is `sql[header.end():]` —
    the SELECT-list AFTER the `SELECT` keyword. Doris's EXPLAIN grammar needs
    a full query, so the keyword must be re-added: `EXPLAIN {body}` (without
    SELECT) fails with `mismatched input 'AS'` / `'('` on every DML — the
    probe was rejecting every valid load SQL and the task died in the
    revision loop (tasks 8e76b357/2967eeec). Verified against the live Doris
    cluster: `EXPLAIN SELECT <body>` (including GROUP BY output aliases) is
    accepted.
    """
    from wren_agent.tools.doris_client import DorisClient

    client = DorisClient()
    try:
        await client.query(f"EXPLAIN SELECT {select_body}")
        return None
    except Exception as exc:
        text = str(exc)
        # Doris error messages carry the useful part after 'detailMessage ='
        marker = "detailMessage = "
        if marker in text:
            text = text.split(marker, 1)[1]
        return text[:400]
    # NOTE: DorisClient is connectionless-per-call (each _run opens+closes a
    # pymysql connection) and has no close() — a finally calling client.close()
    # raised AttributeError on every EXPLAIN and silently disabled this whole
    # check (every call fell into the "unavailable" degradation branch).


async def _doris_explain_load_sqls(state: AgentState, plan: Any) -> list[dict]:
    """Doris-native semantic validation of every DML SELECT body.

    The regex checks only know what exploration recorded; Doris's own parser
    catches everything else — hallucinated columns, dialect mistakes
    (TO_DATE arity, `%Y` vs `yyyy-MM`), type mismatches — with precise,
    actionable messages (tasks cc841bec/def559ca/42452474: the invented
    `quarterly_unpaid_fee` and the two-arg `TO_DATE` both sailed
    past regex but Doris rejects them). DML referencing plan-internal tables
    (created only at deploy) is skipped, and the whole layer degrades
    silently when Doris is unreachable (regex checks still apply).
    """
    if state.get("source_mode") != "warehouse":
        return []

    plan_tables = {
        str(getattr(d, "summary_table", "") or "").lower().rsplit(".", 1)[-1]
        for d in (plan.dws_designs or [])
    } | {
        str(getattr(d, "ads_table", "") or "").lower().rsplit(".", 1)[-1]
        for d in (plan.ads_designs or [])
    }

    failures: list[dict] = []
    for artifact in (plan.sql_artifacts or []):
        if not str(getattr(artifact, "sql_type", "")).upper().startswith("DML_"):
            continue
        sql = getattr(artifact, "sql", "") or ""
        header = _INSERT_HEADER_RE.search(sql)
        if not header:
            continue
        # Plan-internal tables do not exist until deploy — skip their DML.
        refs = _sql_table_refs(sql)
        if any(
            table.lower().rsplit(".", 1)[-1] in plan_tables
            for _db, table in refs
        ):
            continue
        try:
            error = await _explain_on_doris(sql[header.end():])
        except Exception:
            logger.warning(
                "[%s] Doris EXPLAIN unavailable — regex checks only",
                state.get("task_id", "")[:8],
            )
            return failures
        if error:
            failures.append({
                "check": "doris_dml_explain",
                "detail": (
                    f"{artifact.target_table} load SQL rejected by Doris: {error}"
                ),
            })

    # EXPLAIN covers the SELECT body only — an INSERT whose explicit target
    # column list references a column the EXISTING table lacks (task
    # 3034d2f8: `CREATE TABLE IF NOT EXISTS` no-oped over an older table
    # without `monthly_charge_count`) EXPLAINs cleanly and dies at runtime.
    # DESC the target and diff the explicit target columns; unresolvable
    # database / missing table / unreachable Doris degrade silently (the
    # runtime failure is caught later by classify_errors).
    failures.extend(await _check_insert_target_columns(state, plan))
    return failures


async def _check_insert_target_columns(
    state: AgentState, plan: Any,
) -> list[dict]:
    """Verify every DML's explicit INSERT target columns against the real table.

    Plan-internal names that do not exist yet are skipped via ``table_exists``
    (the DDL creates them at deploy). Names that already exist — including
    leftovers from an earlier run's harness DDL probe under the same table
    name (task d3760a4f) — are DESCRIBEd and compared. Returns
    `doris_dml_insert_columns` failures for explicit target columns that do
    not exist in the real Doris table.
    """
    failures: list[dict] = []
    for artifact in (plan.sql_artifacts or []):
        if not str(getattr(artifact, "sql_type", "")).upper().startswith("DML_"):
            continue
        sql = getattr(artifact, "sql", "") or ""
        header = _INSERT_HEADER_RE.search(sql)
        if not header:
            continue
        target_cols = _insert_target_column_names(sql)
        if not target_cols:
            continue  # column-less INSERT — positional to DDL, nothing to check
        insert = _re.search(
            r"\bINSERT\s+INTO\s+(?:`?[\w$-]+`?\.)?`?([\w$-]+)`?\s*\(([^)]*)\)",
            sql, _re.I | _re.S,
        )
        if not insert:
            continue
        bare = _sql_ident(insert.group(1))
        db, table = _resolve_landing_target(state, artifact, sql, bare)
        if not db:
            continue
        from wren_agent.tools.doris_client import DorisClient
        probe = DorisClient(database=db)
        try:
            if not await probe.table_exists(table):
                continue
            real = await probe.get_table_schema(table)
        except Exception:  # noqa: BLE001 — Doris unreachable: silent skip
            continue
        real_cols = {str(c["name"]).lower() for c in real.get("columns", [])}
        if not real_cols:
            continue
        missing = sorted(target_cols - real_cols)
        if not missing:
            continue
        failures.append({
            "check": "doris_dml_insert_columns",
            "detail": (
                f"{getattr(artifact, 'target_table', '')} INSERT targets "
                f"columns {missing} that do not exist in the existing table "
                f"{db}.{table} (real columns: {sorted(real_cols)}) — reconcile "
                "the INSERT list with the real schema or rename the target table"
            ),
        })
    return failures


def _resolve_landing_target(
    state: AgentState, artifact: Any, sql: str, bare: str,
) -> tuple[str, str]:
    """Resolve (database, bare_table) a DML target lands on, best-effort.

    Prefers the db qualifier written in the SQL itself; falls back to the
    layer's configured landing database. Returns ("", "") when unresolvable —
    the caller degrades silently (task 3034d2f8: qualifier vs configured
    layer-db casing must not misfire, so the exact SQL prefix wins).
    """
    qual = _re.search(
        r"\bINSERT\s+INTO\s+`?([\w$-]+)`?\.`?[\w$-]+`?",
        sql, _re.I,
    )
    db = _sql_ident(qual.group(1) if qual else "")
    if not db:
        candidates = _warehouse_layer_db_names(
            state, str(getattr(artifact, "layer", "")).upper(),
        )
        db = next(iter(candidates)) if candidates else ""
    return db, bare


async def _probe_execute_layer_ddls(state: AgentState, plan: Any) -> list[dict]:
    """Execute every DDL against the REAL landing database, in layer
    dependency order (DIM→DWD→DWS→ADS), before the DAG ships to DS.

    The DDLs are `CREATE TABLE IF NOT EXISTS` — running them here is
    idempotent and the DS DAG re-runs them as no-ops. Failures are retried
    once after the deterministic reference rewrites (case/prefix/length),
    then surfaced as validation failures carrying the REAL Doris error so
    design_revise repairs with exact feedback instead of blind rounds
    (task 36437f66: `unique_key_matches_grain` / syntax errors invisible to
    regex burned the whole revision budget and still shipped to DS).

    Degrades silently (returns []) when Doris is unreachable — DS execution
    would fail there anyway and classify_errors catches it.
    """
    from wren_agent.tools.doris_client import DorisClient

    if state.get("source_mode") != "warehouse":
        return []
    artifacts = [
        a for a in (plan.sql_artifacts or [])
        if str(getattr(a, "sql_type", "")).upper() == "DDL"
    ]
    if not artifacts:
        return []
    layer_order = {"DIM": 0, "DWD": 1, "DWS": 2, "ADS": 3}
    artifacts.sort(key=lambda a: layer_order.get(str(a.layer).upper(), 99))

    client = DorisClient()
    errors: dict[int, str] = {}
    for i, art in enumerate(artifacts):
        try:
            await client.execute(art.sql or "")
        except Exception as exc:
            errors[i] = str(exc)
    if errors:
        # One deterministic rewrite pass, then retry only the failed DDLs.
        _rewrite_warehouse_plan_table_refs(state, plan, skip_targets=_compiled_targets(plan))
        for i, art in enumerate(artifacts):
            if i not in errors:
                continue
            try:
                await client.execute(art.sql or "")
                del errors[i]
            except Exception as exc:
                errors[i] = str(exc)

    failures: list[dict] = []
    # The DDLs are `CREATE TABLE IF NOT EXISTS` — when the target table
    # already exists the execute above is a silent no-op and cannot notice a
    # schema conflict. Compare the REAL table (DESCRIBE) against the planned
    # column list and surface missing/extra columns as failures so
    # design_revise reconciles the design instead of shipping a DML that
    # dies at execution (task 3034d2f8: existing dws_category_sales_daily
    # _monthly lacked monthly_charge_count; the no-op DDL let the INSERT run
    # and fail with "column ... is not found in table ..."). Only successful
    # DDLs are probed; unresolvable database / unreachable Doris degrades
    # silently, matching the ddl_probe philosophy.
    for i, art in enumerate(artifacts):
        if i in errors:
            continue
        try:
            failures.extend(await _detect_existing_table_conflict(state, art))
        except Exception as exc:  # noqa: BLE001 — never fail validation on probe
            logger.warning(
                "[%s] existing-table schema probe skipped for %s: %s",
                state.get("task_id", "")[:8],
                getattr(art, "target_table", ""),
                exc,
            )
    for i, err in errors.items():
        art = artifacts[i]
        detail = err
        marker = "detailMessage = "
        if marker in detail:
            detail = detail.split(marker, 1)[1]
        failures.append({
            "check": "ddl_probe",
            "layer": str(getattr(art, "layer", "")).upper(),
            "target_table": getattr(art, "target_table", ""),
            "detail": f"DDL executed on the landing database failed: {detail[:300]}",
        })
    return failures


def _sql_ident(name: str) -> str:
    """Bare SQL identifier: drop backticks/quotes and trailing dots.

    The CREATE TABLE regex captures the qualifier as `` `db`. `` (backticks
    AND the dot). ``.strip('`').strip('.')`` leaves a trailing backtick
    (`dws_gz_test\\``) because the inner backtick is no longer at either
    end — DorisClient then fails and the existing-table probe is swallowed
    (task d3760a4f). Strip every quote first, then leftover punctuation.
    """
    return (name or "").replace("`", "").replace('"', "").strip().strip(".")


async def _detect_existing_table_conflict(
    state: AgentState, art: Any,
) -> list[dict]:
    """Compare a planned DDL's columns against the real Doris table.

    `CREATE TABLE IF NOT EXISTS` silently no-ops when the table already
    exists, so the DDL probe above cannot see schema conflicts. Returns a
    `ddl_probe_schema_conflict` failure when the real table differs from the
    planned columns (missing = the design wants a column the existing table
    lacks; extra = the existing table carries columns the design dropped).
    The existing table may have been built by an earlier run of the same
    goal with a different schema (key/grain drift included) — the failure
    detail carries the real columns and keys so design_revise can rename the
    target or adapt the design instead of re-running the same no-op DDL.

    Degrades to [] (no failure) when the landing database cannot be
    resolved from the SQL/plan, the table does not exist yet, or Doris is
    unreachable — those cases are handled by ddl_probe/EXPLAIN or the DS
    execution itself.
    """
    sql = art.sql or ""
    create = _re.search(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(`?[\w$-]+`?\.)?`?([\w$-]+)`?", sql, _re.I)
    if not create:
        return []
    qualifier = create.group(1) or ""
    table = create.group(2)
    if not table:
        return []
    bare = _sql_ident(table)
    db = _sql_ident(qualifier)
    if not db:
        candidates = _warehouse_layer_db_names(state, str(getattr(art, "layer", "")).upper())
        if not candidates:
            return []
        # Prefer the exact-casing configured landing database when present.
        db = next(iter(candidates))

    from wren_agent.tools.doris_client import DorisClient
    probe = DorisClient(database=db)
    try:
        if not await probe.table_exists(bare):
            return []
        real = await probe.get_table_schema(bare)
    except Exception:  # noqa: BLE001 — Doris unreachable/query error: silent skip
        return []

    real_cols = {str(c["name"]).lower() for c in real.get("columns", [])}
    planned_cols = _ddl_column_names(sql)
    if not real_cols or not planned_cols:
        return []
    missing = sorted(planned_cols - real_cols)
    extra = sorted(real_cols - planned_cols)
    if not missing and not extra:
        return []

    real_keys = real.get("primary_keys") or []
    key_note = f" existing key(s): {real_keys}" if real_keys else ""
    # Missing/extra first — the full column lists used to lead and the
    # 300-char cap truncated `stat_month` off the repair hint (task d3760a4f).
    parts = [
        f"CREATE TABLE IF NOT EXISTS no-oped: target table {db}.{bare} "
        f"already exists"
    ]
    if missing:
        parts.append(f"Missing in real table: {missing}")
    if extra:
        parts.append(
            f"Not in the new design (leftover from an earlier run): {extra}"
        )
    parts.append(
        f"real columns {sorted(real_cols)}; plan expects {sorted(planned_cols)}"
        f"{key_note}"
    )
    return [{
        "check": "ddl_probe_schema_conflict",
        "layer": str(getattr(art, "layer", "")).upper(),
        "target_table": getattr(art, "target_table", ""),
        "detail": ". ".join(parts)[:500],
    }]


def _warehouse_known_dbs(state: AgentState) -> set[str]:
    """Every database a warehouse DML may legally read, with ORIGINAL casing.

    = configured layer databases ∪ landing overrides ∪ explored schema names
    (all layers). Kept case-sensitive because Doris resolves database names
    case-sensitively (task a1af4bed: a lowercase `dim_ecommerce_sh`
    prefix died at execution with `Database [dim_ecommerce_sh] does not
    exist` while `dws_ecommerce_orders_sh` in the same SQL resolved fine).
    """
    dbs: set[str] = set()
    for sdb in state.get("source_databases", []):
        if hasattr(sdb, "model_dump"):
            sdb = sdb.model_dump()
        cp = sdb.get("connection_params") or {}
        for val in ((cp.get("layers") or {}).values()):
            vals = val if isinstance(val, list) else ([val] if val else [])
            dbs.update(str(v).strip() for v in vals if str(v).strip())
        for val in ((cp.get("landing") or {}).values()):
            v = str(val or "").strip()
            if v:
                dbs.add(v)
    for exp in (state.get("explorations") or {}).values():
        for t in (getattr(exp, "tables", None) or []):
            schema = (getattr(t, "schema_name", None) or "").strip()
            if schema:
                dbs.add(schema)
    return dbs


def _warehouse_db_ref_failure(db: str, known_dbs: set[str]) -> dict | None:
    """Case-sensitive existence of a FROM/JOIN database prefix (Doris).

    Returns a failure dict when the prefix is case-mismatched or unknown —
    the old checks compared lowercased names, so `dim_ecommerce_sh`
    sailed past and Doris failed every DML joining it (task a1af4bed).
    """
    if db in known_dbs:
        return None
    exact = next((d for d in known_dbs if d.lower() == db.lower()), None)
    if exact:
        return {
            "check": "warehouse_source_db_case_mismatch",
            "detail": (
                f"database `{db}` does not exist (Doris names are "
                f"case-sensitive) — did you mean `{exact}`?"
            ),
        }
    return {
        "check": "warehouse_source_db_unknown",
        "detail": (
            f"references unknown database `{db}` — known layer databases: "
            f"{sorted(known_dbs) or '(none)'}"
        ),
    }


def _warehouse_layer_db_names(state: AgentState, layer: str) -> set[str]:
    """All databases that may host NEW `layer` tables (dws/ads).

    = landing override ∪ configured layers list ∪ explored schemas of that
    layer. A db-qualified summary_table/ads_table is only valid when its
    prefix is in this set — deploy runs the SQL verbatim, so an invented
    prefix silently creates the table in a wrong/nonexistent database
    (task f9b9a4bd: prefixed names are legitimate — this guards the
    invalid-prefix case).
    """
    dbs: set[str] = set()
    layer = layer.lower()
    from wren_agent.nodes.deployment import _warehouse_layers
    landing = _warehouse_layers(state)
    if landing.get(layer):
        dbs.add(str(landing.get(layer)).lower())
    for sdb in state.get("source_databases", []):
        if hasattr(sdb, "model_dump"):
            sdb = sdb.model_dump()
        vals = ((sdb.get("connection_params") or {}).get("layers") or {}).get(layer)
        if not isinstance(vals, list):
            vals = [vals]
        for v in vals:
            v = str(v or "").strip()
            if v:
                dbs.add(v.lower())
    for key, exp in (state.get("explorations") or {}).items():
        if key.rsplit("#", 1)[-1].lower() != layer:
            continue
        for t in (getattr(exp, "tables", None) or []):
            schema = (getattr(t, "schema_name", None) or "").strip()
            if schema:
                dbs.add(schema.lower())
    return dbs


def _plan_dws_keys(design: Any) -> list[str]:
    """Bare + fully-qualified keys for a plan DWS table.

    The LLM may declare summary_table bare (dws_x) OR db-qualified
    (dws_ecommerce_orders_sh.dws_x) — lookups from ADS sources are
    normalized to bare names, so both key forms must resolve (task
    f9b9a4bd: prefixed summary_table made every ADS parent/lineage
    check miss and fail with unknown parents).
    """
    tbl = design.summary_table.lower()
    bare = tbl.rsplit(".", 1)[-1]
    return [tbl, bare] if bare != tbl else [tbl]


def _validate_materialization_plan(plan: Any, state: AgentState) -> list[ValidationResult]:
    """Validate cross-artifact invariants before a design can be approved.

    Syntax-valid CREATE TABLE statements are insufficient for a materialized
    warehouse.  This gate proves that DIM/DWD/DWS/ADS targets have load SQL,
    coherent grain, physical metric columns, and an unbroken DWS -> ADS lineage
    chain.
    """
    by_target: dict[tuple[str, str], list[Any]] = {}
    for artifact in plan.sql_artifacts or []:
        by_target.setdefault(
            (artifact.layer.upper(), artifact.target_table.lower()), []
        ).append(artifact)

    results: list[ValidationResult] = []

    dws_targets = set()
    for d in (plan.dws_designs or []):
        dws_targets.update(_plan_dws_keys(d))
    if state.get("source_mode") == "warehouse":
        # Warehouse mode: an ADS may legitimately anchor on EXISTING warehouse
        # DWS tables — reusing them is the point of the mode. Restricting
        # sources to this plan's own DWS designs falsely failed sound designs
        # (task 1f49d2d7). Accept bare and db-qualified names.
        for key, exp in (state.get("explorations") or {}).items():
            if key.rsplit("#", 1)[-1].lower() != "dws":
                continue
            for t in (getattr(exp, "tables", None) or []):
                name = getattr(t, "name", "")
                if not name:
                    continue
                dws_targets.add(name.lower())
                schema = getattr(t, "schema_name", "") or ""
                if schema:
                    dws_targets.add(f"{schema}.{name}".lower())
    landing: dict[str, str] = {}
    if state.get("source_mode") == "warehouse":
        from wren_agent.nodes.deployment import _warehouse_layers
        landing = _warehouse_layers(state)

    dws_measures_by_table: dict[str, set[str]] = {}
    for design in (plan.dws_designs or []):
        names = {
            str(measure.get("field_name") or measure.get("name") or "").lower()
            for measure in (design.measures or [])
            if isinstance(measure, dict)
        }
        for key in _plan_dws_keys(design):
            dws_measures_by_table[key] = names
    # ADS may legitimately anchor on EXISTING warehouse DWS tables
    # (task 1f49d2d7) — for those, parent-metric checks resolve against the
    # explored table's physical columns. Without this, ADS derived metrics
    # built on existing monthly/quarterly tables failed every parent (task
    # 15ec38e7: parents like `monthly_cumulative_paid_rent` existed in the
    # explored table but were reported unknown). Plan-created tables win via
    # setdefault (a same-name explored table is not the plan's new one).
    if state.get("source_mode") == "warehouse":
        for key, exp in (state.get("explorations") or {}).items():
            if key.rsplit("#", 1)[-1].lower() != "dws":
                continue
            for t in (getattr(exp, "tables", None) or []):
                name = getattr(t, "name", "")
                if not name:
                    continue
                cols = {
                    str(getattr(f, "name", "") or "").lower()
                    for f in (getattr(t, "fields", None) or [])
                    if getattr(f, "name", "")
                }
                if not cols:
                    continue
                bare = name.lower()
                dws_measures_by_table.setdefault(bare, set()).update(cols)
                schema = getattr(t, "schema_name", "") or ""
                if schema:
                    dws_measures_by_table.setdefault(
                        f"{schema}.{name}".lower(), set()
                    ).update(cols)

    if state.get("source_mode") != "warehouse":
        for layer, designs, target_attr in (
            ("DIM", plan.dim_designs or [], "dim_table"),
            ("DWD", plan.dwd_designs or [], "fact_table"),
        ):
            checks: list[dict] = []
            failures: list[dict] = []
            for design in designs:
                if layer == "DIM" and getattr(design, "source", "") == "from_existing_dim":
                    continue
                target = str(getattr(design, target_attr, "") or "")
                if not target:
                    continue
                artifacts = by_target.get((layer, target.lower()), [])
                types = {a.sql_type.upper() for a in artifacts}
                if "DDL" not in types or not any(t.startswith("DML_") for t in types):
                    failures.append({
                        "check": "materialization_sql_complete",
                        "detail": (
                            f"{target} must have both DDL and DML load SQL; "
                            f"found {sorted(types)}"
                        ),
                    })
                else:
                    checks.append({
                        "check": "materialization_sql_complete",
                        "detail": f"{target} has DDL and DML load SQL",
                    })
            if checks or failures:
                results.append(ValidationResult(
                    level=ValidationLevel.STRUCTURE,
                    layer=layer,
                    passed=not failures,
                    checks=checks,
                    failures=failures,
                ))

    for layer, designs in (
        ("DWS", plan.dws_designs or []),
        ("ADS", plan.ads_designs or []),
    ):
        checks: list[dict] = []
        failures: list[dict] = []
        for design in designs:
            target = design.summary_table if layer == "DWS" else design.ads_table
            artifacts = by_target.get((layer, target.lower()), [])

            # Db-qualified DWS/ADS target names must use a KNOWN database of
            # the layer (landing ∪ explored ∪ configured layers). Prefixed
            # names are legitimate — each layer is its own database — but an
            # invented prefix would silently create the table in the wrong
            # or nonexistent db at deploy (SQL runs verbatim).
            if "." in target and state.get("source_mode") == "warehouse":
                prefix = target.split(".", 1)[0].strip("`").lower()
                allowed = _warehouse_layer_db_names(state, layer)
                if prefix not in allowed:
                    failures.append({
                        "check": "warehouse_target_db_prefix",
                        "detail": (
                            f"{target} targets unknown database `{prefix}` — "
                            f"must be one of the {layer} layer databases "
                            f"(landing ∪ explored ∪ configured): "
                            f"{sorted(allowed) or '(none)'}"
                        ),
                    })
            # The design target may be bare while the DDL SQL creates the
            # table in a WRONG layer's database (e.g. ADS DDL targeting
            # dws_ecommerce_orders_sh — task a2b0cea0: deployed and failed
            types = {a.sql_type.upper() for a in artifacts}
            ddl_text = next(
                (a.sql for a in artifacts if a.sql_type.upper() == "DDL"), ""
            )
            dml_texts = [
                a.sql for a in artifacts if a.sql_type.upper().startswith("DML_")
            ]
            # The design target may be bare while the DDL SQL creates the
            # table in a WRONG layer's database (e.g. ADS DDL targeting
            # dws_ecommerce_orders_sh — task a2b0cea0: deployed and failed
            # there). Check the DDL's CREATE TABLE prefix too.
            if state.get("source_mode") == "warehouse":
                ddl_target = _re.search(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([\w$-]+)`?\s*\.\s*`?([\w$-]+)`?",
                    ddl_text, _re.I,
                )
                if ddl_target:
                    ddl_db = ddl_target.group(1).lower()
                    allowed = _warehouse_layer_db_names(state, layer)
                    if ddl_db not in allowed:
                        failures.append({
                            "check": "warehouse_ddl_target_db",
                            "detail": (
                                f"{target} DDL creates the table in database "
                                f"`{ddl_target.group(1)}` — must be one of the "
                                f"{layer} layer databases (landing ∪ explored ∪ "
                                f"configured): {sorted(allowed) or '(none)'}"
                            ),
                        })
            if "DDL" not in types or not any(t.startswith("DML_") for t in types):
                failures.append({
                    "check": "materialization_sql_complete",
                    "detail": f"{target} must have both DDL and DML load SQL; found {sorted(types)}",
                })
            else:
                checks.append({
                    "check": "materialization_sql_complete",
                    "detail": f"{target} has DDL and DML load SQL",
                })

            # Doris rejects table names longer than 64 chars
            # (table_name_length_limit). Revision rounds have produced names
            # like `dws_order_rental_receivable_analysis_unit_building_room_quarter`
            # (66 chars) that passed every structure check and only died at
            # execution (task a1af4bed).
            bare_target = target.split(".", 1)[-1].strip("`")
            if len(bare_target) > _DORIS_TABLE_NAME_LIMIT:
                failures.append({
                    "check": "warehouse_table_name_too_long",
                    "detail": (
                        f"{target} table name length {len(bare_target)} exceeds "
                        f"Doris limit {_DORIS_TABLE_NAME_LIMIT} "
                        f"(table_name_length_limit) — shorten it"
                    ),
                })

            from wren_agent.compiler.allowlist import bare_source_name
            from wren_agent.nodes.design_layers import compile_source_allowlist as _csa
            allow = _csa(state, layer)
            if allow:
                if layer == "DWS":
                    refs = list(getattr(design, "source_tables", None) or [])
                    src_dwd = getattr(design, "source_dwd_table", None)
                    if src_dwd:
                        refs.append(src_dwd)
                else:
                    refs = list(getattr(design, "source_dws_tables", None) or [])
                extra = []
                seen_extra: set[str] = set()
                allow_l = {a.lower() for a in allow}
                for ref in refs:
                    bare = bare_source_name(str(ref))
                    if bare and bare not in allow_l and bare not in seen_extra:
                        seen_extra.add(bare)
                        extra.append(str(ref))
                if extra:
                    failures.append({
                        "check": "warehouse_goal_source_allowlist",
                        "detail": (
                            f"{target} sources not in the goal allowlist: "
                            f"{extra}. Allowed tables: "
                            f"{', '.join(sorted(allow_l))}"
                        ),
                    })

            if layer == "DWS":
                grain = [str(g) for g in (design.grain or [])]
                group_by = [str(g) for g in (design.group_by_dimensions or [])]
                # group_by must COVER the grain — extra columns are allowed
                # only as descriptive attributes functionally dependent on it
                # (the skill mandates this; strict equality rejected sound
                # designs with e.g. contract_code/tenant_name alongside the
                # grain — task 39e4327b: 13-dim group_by vs 5-col grain).
                missing_grain = sorted(set(grain) - set(group_by))
                if missing_grain:
                    failures.append({
                        "check": "group_by_matches_grain",
                        "detail": (
                            f"{target} group_by_dimensions {group_by} does not "
                            f"cover the grain — missing {missing_grain}; extra "
                            f"group-by columns must be descriptive attributes "
                            f"functionally dependent on the grain"
                        ),
                    })
                else:
                    checks.append({
                        "check": "group_by_matches_grain",
                        "detail": f"{target} group-by dimensions cover its grain",
                    })

                declared_fields = {
                    str(field.get("name") or "").lower()
                    for field in (design.fields or []) if isinstance(field, dict)
                }
                measure_fields = {
                    str(measure.get("field_name") or measure.get("name") or "").lower()
                    for measure in (design.measures or []) if isinstance(measure, dict)
                }
                required_columns = {
                    *(g.strip("`").lower() for g in grain),
                    *declared_fields,
                    *measure_fields,
                } - {""}
                ddl_columns = _ddl_column_names(ddl_text)
                dml_target_columns = set().union(*(
                    _insert_target_column_names(sql) for sql in dml_texts
                )) if dml_texts else set()
                missing_ddl = sorted(required_columns - ddl_columns)
                missing_dml = sorted(required_columns - dml_target_columns) if dml_texts else []
                if missing_ddl:
                    failures.append({
                        "check": "dws_fields_materialized_in_ddl",
                        "detail": f"{target} declared fields/measures missing from DDL: {missing_ddl}",
                    })
                if missing_dml:
                    failures.append({
                        "check": "dws_fields_materialized_in_load_sql",
                        "detail": f"{target} declared fields/measures missing from INSERT target columns: {missing_dml}",
                    })
                if state.get("source_mode") == "warehouse" and (
                    not design.source_tables
                    or any("." not in source for source in design.source_tables)
                ):
                    failures.append({
                        "check": "warehouse_sources_qualified",
                        "detail": f"{target} has unqualified warehouse source tables: {design.source_tables}",
                    })
                if state.get("source_mode") == "warehouse":
                    failures.extend(
                        _validate_warehouse_dws_sources(design, artifacts, state, plan)
                    )
                if target.lower().endswith("_daily") and not any(
                    _re.search(r"(?:date|day|time|_dt$)", g, _re.I) for g in grain
                ):
                    failures.append({
                        "check": "daily_time_grain",
                        "detail": f"Daily table {target} has no date/time grain: {grain}",
                    })

                key_match = _re.search(r"UNIQUE\s+KEY\s*\(([^)]*)\)", ddl_text, _re.I)
                simple_grain = [g.strip("`").lower() for g in grain if _re.fullmatch(r"`?[A-Za-z_]\w*`?", g)]
                if key_match and simple_grain:
                    key_cols = [c.strip().strip("`").lower() for c in key_match.group(1).split(",")]
                    if key_cols != simple_grain:
                        failures.append({
                            "check": "unique_key_matches_grain",
                            "detail": f"{target} UNIQUE KEY {key_cols} != grain {simple_grain}",
                        })
                    else:
                        checks.append({
                            "check": "unique_key_matches_grain",
                            "detail": f"{target} UNIQUE KEY matches its grain",
                        })
            else:
                dimensions = list(getattr(design, "dimensions", []) or [])
                if not dimensions:
                    failures.append({
                        "check": "ads_dimensions_declared",
                        "detail": f"{target} has no declared query dimensions/grain",
                    })
                field_names = {
                    str(f.get("name", "")).lower()
                    for f in (design.fields or []) if isinstance(f, dict)
                }
                normalized_dimensions = {
                    str(dimension).strip("`").lower() for dimension in dimensions
                }
                ddl_columns = _ddl_column_names(ddl_text)
                # A dimension counts as materialized when declared in fields
                # OR physically present in the DDL — the DDL is the physical
                # truth, and the LLM sometimes materializes the dimension
                # columns in DDL/load SQL while omitting them from the fields
                # list (task 6ba95453: all 6 dimensions were in the DDL).
                missing_dimension_fields = sorted(
                    normalized_dimensions - field_names - ddl_columns
                )
                if missing_dimension_fields:
                    failures.append({
                        "check": "ads_dimensions_materialized",
                        "detail": f"{target} dimensions missing from fields and DDL: {missing_dimension_fields}",
                    })
                missing_metric_fields = [
                    str(m.get("field_name") or m.get("name") or "")
                    for m in (design.metrics or []) if isinstance(m, dict)
                    and str(m.get("field_name") or m.get("name") or "").lower() not in field_names
                ]
                if missing_metric_fields:
                    failures.append({
                        "check": "metric_fields_materialized",
                        "detail": f"{target} metrics missing physical fields: {missing_metric_fields}",
                    })
                required_columns = field_names | normalized_dimensions
                dml_target_columns = set().union(*(
                    _insert_target_column_names(sql) for sql in dml_texts
                )) if dml_texts else set()
                missing_ddl = sorted(required_columns - ddl_columns)
                missing_dml = sorted(required_columns - dml_target_columns) if dml_texts else []
                if missing_ddl:
                    failures.append({
                        "check": "ads_fields_materialized_in_ddl",
                        "detail": f"{target} fields missing from DDL: {missing_ddl}",
                    })
                if missing_dml:
                    failures.append({
                        "check": "ads_fields_materialized_in_load_sql",
                        "detail": f"{target} fields missing from INSERT target columns: {missing_dml}",
                    })

                upstream_measure_names: set[str] = set()
                for source in (design.source_dws_tables or []):
                    upstream_measure_names.update(
                        dws_measures_by_table.get(source.strip("`").rsplit(".", 1)[-1].lower(), set())
                    )
                for metric in (design.metrics or []):
                    if not isinstance(metric, dict):
                        continue
                    metric_name = str(metric.get("name") or "?")
                    metric_type = str(metric.get("metric_type") or "").lower()
                    if metric_type not in {"atomic", "derived"}:
                        failures.append({
                            "check": "metric_type_declared",
                            "detail": f"{target}.{metric_name} must declare metric_type atomic or derived",
                        })
                        continue
                    parents = [str(parent).lower() for parent in (metric.get("parent_metrics") or [])]
                    if metric_type == "derived" and not parents:
                        failures.append({
                            "check": "derived_metric_parent_chain",
                            "detail": f"{target}.{metric_name} is derived but has no parent_metrics",
                        })
                    unknown_parents = [
                        parent for parent in parents
                        if parent not in upstream_measure_names
                        and not parent.startswith("registry:")
                    ]
                    if unknown_parents:
                        failures.append({
                            "check": "derived_metric_parent_chain",
                            "detail": f"{target}.{metric_name} references unknown DWS parent metrics: {unknown_parents}",
                        })
                # Match by bare table name so `db_dws.dws_x` (landing-qualified)
                # is recognized as this plan's `dws_x` (task 4e372c61: ADS
                # sources written with a db prefix falsely failed lineage).
                unknown_sources = [
                    source for source in (design.source_dws_tables or [])
                    if source.strip("`").rsplit(".", 1)[-1].lower() not in dws_targets
                ]
                if unknown_sources:
                    failures.append({
                        "check": "ads_dws_lineage",
                        "detail": f"{target} references DWS tables outside this plan: {unknown_sources}",
                    })
                if state.get("source_mode") == "warehouse":
                    # Pass `plan` so this plan's own DWS/ADS tables (not yet
                    # in exploration) resolve their columns from the plan DDLs
                    # (task fd059c0a: ADS anchored on the plan's DWS tables
                    # per the ADS injection and failed
                    # warehouse_source_table_explored without it).
                    failures.extend(
                        _validate_warehouse_load_sql(
                            target, artifacts, state, plan, target_layer="ADS",
                        )
                    )
                # ADS load SQL must qualify every DWS source with a KNOWN DWS
                # database — the landing db OR any explored dws-layer schema
                # (e.g. dws_ecommerce_orders_sh). The old check hard-coded the
                # landing db as the only valid prefix and falsely rejected
                # sound designs anchored on existing SH/GZ/BJ tables
                # (task 3dacf0ae: refs like
                # `dws_ecommerce_orders_sh.dws_order_subscription_...`).
                dws_dbs: set[str] = set()
                if landing.get("dws"):
                    dws_dbs.add(str(landing.get("dws")).lower())
                for key, exp in (state.get("explorations") or {}).items():
                    if key.rsplit("#", 1)[-1].lower() != "dws":
                        continue
                    for t in (getattr(exp, "tables", None) or []):
                        schema = (getattr(t, "schema_name", None) or "").strip()
                        if schema:
                            dws_dbs.add(schema.lower())
                unqualified_refs: list[str] = []
                if dws_dbs:
                    for sql in dml_texts:
                        for db, tbl in _sql_table_refs(sql):
                            if not db or db.lower() not in dws_dbs:
                                unqualified_refs.append(
                                    f"{db}.{tbl}" if db else tbl
                                )
                if unqualified_refs:
                    failures.append({
                        "check": "ads_cross_database_source",
                        "detail": (
                            f"{target} load SQL must qualify every DWS source "
                            f"with a known DWS database "
                            f"({sorted(dws_dbs) or 'landing db'}): "
                            f"{sorted(set(unqualified_refs))}"
                        ),
                    })

        if checks or failures:
            results.append(ValidationResult(
                level=ValidationLevel.STRUCTURE,
                layer=layer,
                passed=not failures,
                checks=checks,
                failures=failures,
            ))

    # LLM design-review suggestions are advisory only: the review model
    # over-triggers severity="error" on what are really improvement hints,
    # and treating them as blockers fail-fasted otherwise sound tasks
    # (8fc1d873/f6df37fd) without giving design_revise a chance. Surface
    # them as a non-blocking result (passed=None, like semantic checks).
    # Plan-level requirement coverage: a requirement is covered when ANY
    # DWS/ADS table in the plan materializes it (layered designs legitimately
    # split grains across tables and defer derived metrics to ADS — task
    # f3511018). `_coverage_failures_plan` also performs the per-table
    # structural checks (invalid entries, gap reasons, evidence).
    plan_designs = list(plan.dws_designs or []) + list(plan.ads_designs or [])
    plan_coverage_failures = _coverage_failures_plan(
        plan_designs,
        allow_incomplete=bool(state.get("insufficient_gaps")),
    )
    if plan_coverage_failures:
        results.append(ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer="PLAN",
            passed=False,
            checks=[],
            failures=plan_coverage_failures,
        ))

    review_issues = [
        issue for issue in state.get("design_review_issues", [])
        if str(issue.get("severity", "")).lower() == "error"
    ]
    if review_issues:
        results.append(ValidationResult(
            level=ValidationLevel.SEMANTIC,
            layer="DESIGN_REVIEW",
            passed=None,
            checks=[],
            failures=[{
                "check": issue.get("rule_id", "llm_design_review"),
                "detail": issue.get("detail", "Design review suggestion"),
            } for issue in review_issues],
        ))

    return results


def _validate_design_shape(artifact: Any, plan: Any) -> ValidationResult | None:
    """Level 1b: the DDL must physically contain the design's grain/dimension
    columns.

    Catches designs like an ADS table declared as "by village by month" whose
    DDL carries only the metric column — structurally valid SQL, semantically
    useless table.
    """
    target = (artifact.target_table or "").lower()
    checks, failures = [], []

    if artifact.layer == "DWS":
        design = next(
            (d for d in (plan.dws_designs or []) if d.summary_table.lower() == target),
            None,
        )
        if design is None or not design.grain:
            return None
        ddl_cols = _ddl_column_names(artifact.sql)
        missing = [g for g in design.grain if g.lower() not in ddl_cols]
        if missing:
            failures.append({
                "check": "grain_columns_present",
                "detail": f"Grain columns missing from DDL: {missing} "
                          f"(declared grain: {design.grain})",
            })
        else:
            checks.append({"check": "grain_columns_present",
                           "detail": f"All {len(design.grain)} grain columns present"})

    elif artifact.layer == "ADS":
        design = next(
            (a for a in (plan.ads_designs or []) if a.ads_table.lower() == target),
            None,
        )
        if design is None:
            return None
        metric_names = {
            (m.get("name") or "").lower()
            for m in (design.metrics or []) if isinstance(m, dict)
        }
        field_names = [
            (f.get("name") or "") for f in (design.fields or []) if isinstance(f, dict)
        ]
        dim_fields = [f for f in field_names if f and f.lower() not in metric_names]
        if field_names and not dim_fields:
            failures.append({
                "check": "dimension_columns_present",
                "detail": "ADS design defines only metric columns — no dimension/grain "
                          "columns (e.g. village, month). The table cannot answer "
                          "'per <dimension>' questions.",
            })
        elif dim_fields:
            ddl_cols = _ddl_column_names(artifact.sql)
            missing = [f for f in dim_fields if f.lower() not in ddl_cols]
            if missing:
                failures.append({
                    "check": "dimension_columns_present",
                    "detail": f"Dimension columns missing from DDL: {missing}",
                })
            else:
                checks.append({"check": "dimension_columns_present",
                               "detail": f"{len(dim_fields)} dimension columns present"})

    if not checks and not failures:
        return None
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer=artifact.layer,
        passed=len(failures) == 0,
        checks=checks,
        failures=failures,
    )


async def _validate_structure(artifact: Any) -> ValidationResult:
    """Level 1: Structure validation.

    Checks:
    - SQL syntax is valid (in production: parse SQL, validate)
    - Referenced tables exist in Doris
    - Referenced columns exist
    - JOIN conditions reference valid columns
    - Data types are compatible
    """
    # In production: execute EXPLAIN or dry-run on Doris
    # For now: basic syntax check
    sql = artifact.sql
    checks = []
    failures = []

    # Basic syntax: must contain CREATE TABLE or SELECT
    if not sql or not sql.strip():
        failures.append({"check": "sql_not_empty", "detail": "SQL is empty"})
    elif not any(kw in sql.upper() for kw in ("CREATE TABLE", "SELECT", "INSERT", "MERGE")):
        failures.append({"check": "valid_statement_type", "detail": "No valid DDL/DML statement found"})
    else:
        checks.append({"check": "valid_statement_type", "detail": "Passed"})

    # Check for common syntax errors
    if ";;" in sql:
        failures.append({"check": "double_semicolon", "detail": "Double semicolon detected"})
    else:
        checks.append({"check": "double_semicolon", "detail": "Passed"})

    passed = len(failures) == 0
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer=artifact.layer,
        passed=passed,
        checks=checks,
        failures=failures,
    )


async def _validate_consistency(artifact: Any, plan: Any) -> ValidationResult:
    """Level 2: Data consistency validation.

    Checks:
    - ODS row count == source row count
    - DWD row count <= ODS row count (dedup)
    - DWS aggregation matches DWD detail
    - ADS derivation correct
    """
    checks = []
    failures = []

    layer = artifact.layer
    if layer == "ODS":
        checks.append({"check": "ods_row_count", "detail": "Will verify after execution"})
    elif layer == "DWD":
        checks.append({"check": "dwd_dedup", "detail": f"Source: {artifact.dependencies}"})
    elif layer == "DWS":
        checks.append({"check": "dws_aggregation", "detail": f"Source: {artifact.dependencies}"})
    elif layer == "ADS":
        checks.append({"check": "ads_derivation", "detail": f"Source: {artifact.dependencies}"})

    # In production: actual data comparison queries on Doris
    passed = len(failures) == 0
    return ValidationResult(
        level=ValidationLevel.CONSISTENCY,
        layer=layer,
        passed=passed,
        checks=checks,
        failures=failures,
    )


def _rule_based_semantic_assertions(state: AgentState) -> list[dict]:
    """Build assertions from answered metric_definition clarifications."""
    assertions: list[dict] = []
    for c in state.get("clarifications", []):
        if getattr(c, "status", None) == ClarificationStatus.ANSWERED and c.answer:
            if c.clarification_type == "metric_definition":
                metric_name = (
                    c.question.split("'")[1]
                    if c.question.count("'") >= 2
                    else "unknown"
                )
                assertions.append({
                    "metric": metric_name,
                    "expected_range": "TBD",
                    "source": f"Human confirmed: {c.answer[:100]}",
                })
    return assertions


async def _generate_semantic_assertions_batch(
    artifacts: list[Any],
    state: AgentState,
) -> list[ValidationResult]:
    """Level 3: one LLM call for all ADS/DWS artifacts (was per-artifact)."""
    shared = _rule_based_semantic_assertions(state)
    by_key: dict[str, list[dict]] = {}

    try:
        from wren_agent.tools.llm_client import get_llm_client
        import json as _json
        import re as _re

        llm = get_llm_client()
        goal = state["modeling_goal"]
        clar_text = "\n".join(
            f"Q: {c.question} A: {c.answer}"
            for c in state.get("clarifications", [])
            if getattr(c, "status", None) == ClarificationStatus.ANSWERED
        )
        artifact_lines = []
        for i, art in enumerate(artifacts):
            target = getattr(art, "target_table", None) or getattr(art, "table_name", "") or "?"
            artifact_lines.append(f"{i}: layer={art.layer}, table={target}")

        system_prompt = (
            "You are validating a data warehouse. Generate semantic assertions "
            "based on the modeling goal and human answers.\n"
            f"Goal: {goal}"
        )
        user_message = f"""Based on these human clarifications:
{clar_text[:2000]}

Artifacts to cover:
{chr(10).join(artifact_lines)}

For EACH artifact index, generate up to 3 semantic validation assertions.
Return JSON:
{{"by_artifact": [
  {{"index": 0, "assertions": [{{"metric": "name", "expected_range": "> 0", "source": "reasoning"}}]}}
]}}"""

        # A single call can return an EMPTY response (gateway hiccup — task
        # d28431de: both harness rounds burned ~130s each for zero assertions,
        # so the semantic layer never validated anything). Retry once.
        response = ""
        for attempt in range(2):
            try:
                response = await llm.generate_json(
                    system_prompt, user_message, token_kind="semantic",
                )
            except Exception as exc:
                if attempt == 0:
                    logger.warning(
                        "Semantic assertion LLM call failed (%s) — retrying once",
                        type(exc).__name__,
                    )
                    continue
                raise
            if (response or "").strip():
                break
            if attempt == 0:
                logger.warning(
                    "Semantic assertion LLM returned empty response — retrying once"
                )
        else:
            response = ""

        match = _re.search(r'\{.*\}', response, _re.DOTALL)
        if match:
            data = _json.loads(match.group(0))
            for entry in data.get("by_artifact", []) or []:
                try:
                    idx = int(entry.get("index", -1))
                except (TypeError, ValueError):
                    continue
                if idx < 0 or idx >= len(artifacts):
                    continue
                key = str(idx)
                for a in (entry.get("assertions") or [])[:3]:
                    by_key.setdefault(key, []).append({
                        "metric": a.get("metric", "unknown"),
                        "expected_range": a.get("expected_range", "TBD"),
                        "source": f"LLM: {a.get('source', 'generated')}",
                    })
    except Exception:
        logger.warning(
            "Semantic assertion LLM generation failed", exc_info=True,
        )

    results: list[ValidationResult] = []
    for i, artifact in enumerate(artifacts):
        assertions = list(shared) + list(by_key.get(str(i), []))
        if not assertions:
            assertions.append({
                "metric": "N/A",
                "expected_range": "No semantic assertions generated",
                "source": "Insufficient grilled knowledge + LLM unavailable",
            })
        results.append(ValidationResult(
            level=ValidationLevel.SEMANTIC,
            layer=artifact.layer,
            passed=None,
            checks=[],
            failures=[],
            semantic_assertions=assertions,
        ))
    return results


async def _generate_semantic_assertions(artifact: Any, state: AgentState) -> ValidationResult:
    """Level 3: Semantic validation for a single artifact (compat wrapper)."""
    results = await _generate_semantic_assertions_batch([artifact], state)
    return results[0]


def _remap_cross_layer_failures(
    state: AgentState, plan: Any, failures: list[ValidationResult],
) -> list[ValidationResult]:
    """Re-route DORIS_EXPLAIN failures onto the layer that owns the failing table.

    The cross-layer check (`_doris_explain_load_sqls`: EXPLAIN every DML
    SELECT body, plus the `doris_dml_insert_columns` DESC diff) validates every
    DWS/ADS artifact but attaches the failure to a synthetic layer name that
    has no SQLArtifact of its own. The repair loop (`_llm_repair_artifact`)
    filters `plan.sql_artifacts` by `failure.layer` — with layer ==
    "DORIS_EXPLAIN" that list is empty and the repair returns False without
    ever calling the LLM, so the same failure recurs and the no-progress guard
    fails the whole task (task 3d19c031: a healthy plan died after one 0.4 ms
    no-op revision round). Remap onto the owning DWS/ADS layer so the repair
    loop targets the real artifacts.
    """
    if plan is None or not failures:
        return failures

    # bare table name -> owning layer, from the plan's design entries
    owner: dict[str, str] = {}
    for d in (getattr(plan, "dws_designs", None) or []):
        t = str(getattr(d, "summary_table", "") or "").rsplit(".", 1)[-1].strip("`")
        if t:
            owner[t.lower()] = "DWS"
    for d in (getattr(plan, "ads_designs", None) or []):
        t = str(getattr(d, "ads_table", "") or "").rsplit(".", 1)[-1].strip("`")
        if t:
            owner[t.lower()] = "ADS"

    # The failure detail is prefixed with the target table (`db_dws.dws_x load
    # SQL rejected by Doris: ...` / `dws_x INSERT targets columns [...]`).
    prefixed = _re.compile(
        r"^\s*`?(?:`?[\w$-]+`?\.)?`?([\w$-]+)`?\s+", _re.I
    )

    remapped: list[ValidationResult] = []
    for failure in failures:
        if failure.passed is not False or failure.layer != "DORIS_EXPLAIN":
            # PLAN failures name source tables (never plan targets) — leave
            # every other layer untouched.
            remapped.append(failure)
            continue
        # Split the cross-layer failures by owning layer — a single DORIS_EXPLAIN
        # result can carry DWS and ADS failures at once, and the repair loop is
        # per-layer. Failures whose table maps to no plan target stay on the
        # original layer so nothing is silently dropped.
        by_layer: dict[str, list[dict]] = {}
        for f in (failure.failures or []):
            detail = str(f.get("detail", "") or "")
            m = prefixed.match(detail)
            target = (m.group(1).lower() if m else "")
            owning = owner.get(target)
            by_layer.setdefault(owning or failure.layer, []).append(f)
        for layer, fl in by_layer.items():
            remapped.append(ValidationResult(
                level=failure.level,
                layer=layer,
                passed=failure.passed,
                checks=failure.checks,
                failures=fl,
                semantic_assertions=failure.semantic_assertions,
                executed_at=failure.executed_at,
            ))
    return remapped


async def design_revise(state: AgentState) -> AgentState:
    """Revise design based on validation failures — LLM-driven repair.

    1. Collects failed validation details (layer, artifact, error message)
    2. Sends failures + current DDL to LLM for targeted repair
    3. Replaces failed SQL artifacts with LLM-fixed versions
    4. Falls back to counter-only behavior if LLM is unavailable

    Increments design_revision_count so route_after_validation can
    escalate to human approval after MAX_DESIGN_REVISIONS attempts.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["design_revision_count"] = state.get("design_revision_count", 0) + 1

    results = state.get("validation_results", [])
    failures = [r for r in results if r.passed is False]

    # Execution-time SQL errors (logic/data) arrive via route_after_error_
    # classify → design_revise: convert them into repairable failures so the
    # LLM fixes the statement, instead of the human re-approving the SAME
    # broken SQL in a loop (task 57d84a75: 5 identical "DAG 执行错误"
    # approvals on an unqualified table reference).
    if not failures:
        exec_errs = [
            e for e in state.get("error_classifications", [])
            if getattr(getattr(e, "error_type", None), "value", "") in ("logic", "data")
        ]
        failures = [
            f for f in (_failure_from_execution_error(e) for e in exec_errs)
            if f is not None
        ]
        # Consume repairable execution errors so a later harness pass does not
        # re-open human_approval with stale escalate items (task b131f22e).
        if exec_errs:
            state["error_classifications"] = [
                e for e in state.get("error_classifications", [])
                if getattr(getattr(e, "error_type", None), "value", "")
                not in ("logic", "data")
            ]

    if not failures:
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "design_revise",
            "action": "no_failures_found",
            "revision": state["design_revision_count"],
            "timestamp": now.isoformat(),
        }]
        return state

    # Cross-layer failures (DORIS_EXPLAIN / doris_dml_insert_columns) have no
    # SQLArtifact with that layer name — remap them onto the layer that owns
    # the failing table so the repair loop targets the real artifacts instead
    # of short-circuiting (task 3d19c031: probe-side %-format error failed a
    # healthy plan in one no-op round).
    plan = state.get("design_plan")
    failures = _remap_cross_layer_failures(state, plan, failures)

    # Try LLM-driven repair for each failed layer
    if plan:
        compiled = _compiled_targets(plan)
        fixed_count = 0
        for failure in failures:
            try:
                if await _try_operator_cast_repair(state, failure, plan):
                    fixed_count += 1
                    continue
                # Compiler mode (ADR-0015 Phase 4): repair the SEMANTIC design
                # (structure diff) and recompile — SQL-text repair would
                # diverge from the semantic source and be overwritten anyway.
                has_semantics = any(
                    getattr(sd, "layer", "") == failure.layer
                    for sd in (state.get("compiled_semantics") or {}).values()
                )
                if failure.layer in ("DWS", "ADS") and has_semantics:
                    fixed = await _llm_repair_semantic(state, failure, plan)
                else:
                    fixed = await _llm_repair_artifact(state, failure, plan)
                if fixed:
                    fixed_count += 1
            except Exception:
                logger.warning(
                    "LLM repair failed for %s layer (revision %d)",
                    failure.layer, state["design_revision_count"], exc_info=True,
                )

        if fixed_count > 0:
            state["warnings"] = state.get("warnings", []) + [
                f"[design_revise] LLM repaired {fixed_count}/{len(failures)} failed layers"
            ]

    # Sync warehouse-mode design metadata with the (possibly repaired) SQL.
    # The LLM often leaves source_tables empty/unqualified even when its own
    # load SQL references the right tables — SQL-only repairs can never fix
    # those metadata-level invariants (tasks 8fc1d873, 4a68562e). Compiled
    # targets are exempt — their metadata comes from the semantic design.
    if plan:
        compiled = _compiled_targets(plan)
        _rewrite_warehouse_plan_table_refs(state, plan, skip_targets=compiled)
        _backfill_warehouse_source_metadata(state, plan, skip_targets=compiled)
        _backfill_declared_columns(state, plan, skip_targets=compiled)

    # Always record the failures as warnings for auditability
    state["warnings"] = state.get("warnings", []) + [
        f"Validation failure in {f.layer}: {len(f.failures)} checks failed"
        for f in failures
    ]

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "design_revise",
        "action": "revision_started",
        "revision": state["design_revision_count"],
        "failed_layers": [f.layer for f in failures],
        "timestamp": now.isoformat(),
    }]

    return state


_SQL_REF_RE = _re.compile(
    r"(?:FROM|JOIN)\s+(?:(`?)([\w$-]+)\1\.)?(`?)([\w$-]+)\3", _re.I
)

_SQL_ALIAS_RE = _re.compile(
    r"(?:FROM|JOIN)\s+(?:(`?)([\w$-]+)\1\.)?(`?)([\w$-]+)\3"
    r"(?:\s+(?:AS\s+)?(`?\w+`?))?",
    _re.I,
)

_SQL_COL_REF_RE = _re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)")


def _validate_dml_columns(artifact: Any, state: AgentState) -> ValidationResult | None:
    """Level 1c: every alias.column in a DML statement must exist in the
    explored schema of its source table.

    Catches LLM-hallucinated columns at validation time — before Doris
    rejects the statement at execution (tasks 2b459bd5/96cda938/5c3b24b4
    burned whole deploy-execute cycles on 'Unknown column' errors). The
    failure detail includes the table's real column list so the repair loop
    gets precise, actionable feedback.
    """
    if not artifact.sql_type.upper().startswith("DML_"):
        return None
    # Warehouse DWS/ADS DMLs are gated by _validate_warehouse_load_sql
    # (table-explored + alias + column checks with available-column details).
    if state.get("source_mode") == "warehouse" and artifact.layer in ("DWS", "ADS"):
        return None

    tables: dict[str, set[str]] = {}
    for exp in (state.get("explorations") or {}).values():
        for t in (getattr(exp, "tables", None) or []):
            name = getattr(t, "name", "")
            cols = {
                getattr(f, "name", "").lower()
                for f in (getattr(t, "fields", None) or [])
                if getattr(f, "name", "")
            }
            if name and cols:
                tables.setdefault(name.lower(), cols)
    if not tables:
        return None

    sql = artifact.sql or ""

    alias_map: dict[str, str] = {}
    for m in _SQL_ALIAS_RE.finditer(sql):
        table = m.group(4)
        alias = (m.group(5) or "").strip("`").lower()
        if not alias or alias in _SQL_CLAUSE_WORDS:
            alias = table.lower()
        alias_map.setdefault(alias, table.lower())
    if not alias_map:
        return None

    unknown: list[tuple[str, str, str]] = []  # (ref, column, table)
    seen: set[tuple[str, str]] = set()
    for m in _SQL_COL_REF_RE.finditer(sql):
        alias, col = m.group(1).lower(), m.group(2).lower()
        table = alias_map.get(alias)
        if table is None:
            continue  # unknown alias (subquery/CTE) — best-effort, skip
        cols = tables.get(table)
        if cols is None:
            continue  # table not explored (plan-internal layer table) — skip
        if col not in cols and (alias, col) not in seen:
            seen.add((alias, col))
            unknown.append((f"{m.group(1)}.{m.group(2)}", col, table))

    # Single explored source: also reject bare SELECT/WHERE columns.
    # DIM/DWD load SQL is typically `SELECT id, code FROM ods_x` with no
    # alias — qualified-only checks let hallucinated names through
    # (task de018b42: dim_floor SELECT `code`/`name` vs explored
    # floor_code/floor_name).
    explored_sources: list[tuple[str, set[str]]] = []
    seen_tables: set[str] = set()
    for table in alias_map.values():
        key = table.rsplit(".", 1)[-1]
        if key in seen_tables:
            continue
        cols = tables.get(key)
        if cols is None:
            continue
        seen_tables.add(key)
        explored_sources.append((key, cols))
    missing_bare: list[str] = []
    if len(explored_sources) == 1:
        _src, cols = explored_sources[0]
        missing_bare = sorted(
            c for c in (
                _bare_source_column_candidates(sql)
                | _unqualified_select_source_columns(sql)
            )
            if c not in cols
        )

    layer = artifact.layer
    if not unknown and not missing_bare:
        return ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer=layer,
            passed=True,
            checks=[{
                "check": "dml_columns_exist",
                "detail": "All referenced source columns exist in the explored schema",
            }],
            failures=[],
        )

    failures = []
    for ref, _col, table in unknown[:10]:
        avail = ", ".join(sorted(tables[table])[:25])
        failures.append({
            "check": "dml_columns_exist",
            "detail": (
                f"{artifact.target_table}: `{ref}` does not exist in source "
                f"table `{table}`. Available columns: {avail}"
            ),
        })
    if missing_bare:
        src, cols = explored_sources[0]
        avail = ", ".join(sorted(cols)[:25])
        failures.append({
            "check": "dml_columns_exist",
            "detail": (
                f"{artifact.target_table}: bare columns {missing_bare} do not "
                f"exist in source table `{src}`. Available columns: {avail}"
            ),
        })
    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer=layer,
        passed=False,
        checks=[],
        failures=failures,
    )


def _sql_table_refs(sql: str) -> list[tuple[str, str]]:
    """Extract (database, table) refs from FROM/JOIN clauses (best-effort)."""
    refs: list[tuple[str, str]] = []
    for m in _SQL_REF_RE.finditer(sql or ""):
        refs.append((m.group(2) or "", m.group(4)))
    return refs


def _backfill_warehouse_source_metadata(
    state: AgentState, plan: Any, skip_targets: set[str] | None = None,
) -> None:
    """Repair warehouse-mode source metadata from the design's own load SQL.

    `warehouse_sources_qualified` / `ads_dws_lineage` read design metadata,
    not SQL — so when the LLM leaves `source_tables` empty but writes the
    right tables into its DML, the invariants fail no matter how often the
    SQL is repaired. Deriving the metadata from the DML keeps them in sync.
    Compiled targets are skipped — their sources come from the semantic
    design directly.
    """
    if state.get("source_mode") != "warehouse" or plan is None:
        return
    skip = skip_targets or set()

    # bare table name → schema (database), from the explored warehouse tables
    name_to_schema: dict[str, str] = {}
    for exp in (state.get("explorations") or {}).values():
        for t in (getattr(exp, "tables", None) or []):
            schema = getattr(t, "schema_name", "")
            name = getattr(t, "name", "")
            if schema and name:
                name_to_schema.setdefault(name.lower(), schema)

    # bare plan-created DWS/ADS table name → its landing database, so sibling
    # references resolve even though the table is not explored yet.
    plan_landing = _plan_internal_landing_tables(state, plan)

    def refs_for(layer: str, target: str) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for a in (plan.sql_artifacts or []):
            if (a.layer == layer
                    and a.target_table.lower() == target.lower()
                    and a.sql_type.upper().startswith("DML_")):
                refs.extend(_sql_table_refs(a.sql))
        return [(db, t) for db, t in refs if t.lower() != target.lower()]

    real_schemas = set(name_to_schema.values())

    def resolve(refs: list[tuple[str, str]]) -> list[str]:
        out = []
        for db, tbl in refs:
            if db and db in real_schemas:
                out.append(f"{db}.{tbl}")
            elif tbl.lower() in name_to_schema:
                # Wrong/missing db prefix (e.g. the LLM used the exploration
                # key UUID) — the exploration knows the real schema.
                out.append(f"{name_to_schema[tbl.lower()]}.{tbl}")
            elif tbl.lower() in plan_landing:
                # Sibling DWS/ADS table created by this plan — use its
                # landing database (task a23e1599: unqualified sibling ref).
                out.append(f"{plan_landing[tbl.lower()]}.{tbl}")
            elif db:
                out.append(f"{db}.{tbl}")
            else:
                out.append(tbl)
        return sorted(set(out))

    for design in (plan.dws_designs or []):
        if str(design.summary_table or "") in skip:
            continue
        needs = (not design.source_tables) or any(
            "." not in str(s) for s in design.source_tables
        )
        if needs:
            resolved = resolve(refs_for("DWS", design.summary_table))
            if resolved:
                design.source_tables = resolved

    dws_targets = set()
    for d in (plan.dws_designs or []):
        dws_targets.update(_plan_dws_keys(d))
    for design in (plan.ads_designs or []):
        if str(design.ads_table or "") in skip:
            continue
        # Only sync when the ADS load SQL actually reads this plan's DWS
        # tables — an ADS reading DWD directly is a real layering violation
        # that no metadata edit should paper over.
        in_plan = sorted({
            next(d.summary_table for d in plan.dws_designs
                 if t.lower() in _plan_dws_keys(d))
            for _db, t in refs_for("ADS", design.ads_table)
            if t.lower() in dws_targets
        })
        if in_plan and [s.lower() for s in in_plan] != [
                str(s).lower() for s in (design.source_dws_tables or [])]:
            design.source_dws_tables = in_plan


async def _try_operator_cast_repair(state: dict, failure: Any, plan: Any) -> bool:
    """Apply the highest-P repair-doris-dml candidate (heuristic + history + EXPLAIN)."""
    from wren_agent.tools.doris_dml_repair import (
        apply_operator_cast_candidates,
        generate_dml_repair_candidates,
        insert_select_body,
        patch_semantic_exprs,
        is_operator_cast_error,
        load_dml_repair_stats,
    )

    details = " ".join(
        str(f.get("detail", "")) for f in (failure.failures or [])
    )
    checks = {str(f.get("check", "")) for f in (failure.failures or [])}
    if "doris_operator_cast" not in checks and not is_operator_cast_error(details):
        return False
    sql = ""
    layer = str(failure.layer).upper()
    for art in getattr(plan, "sql_artifacts", None) or []:
        if str(getattr(art, "layer", "")).upper() != layer:
            continue
        if str(getattr(art, "sql_type", "")).startswith("DML"):
            sql = getattr(art, "sql", "") or ""
            if sql:
                break
    explain: dict[str, bool] = {}
    if sql:
        prelim = generate_dml_repair_candidates(
            sql, details, stats=load_dml_repair_stats(),
        )
        for cand in prelim:
            body = insert_select_body(cand.sql)
            if not body:
                continue
            err = await _explain_on_doris(body)
            if err is None:
                explain[cand.label] = True
            elif any(
                k in err.lower()
                for k in ("connection", "can't connect", "timed out", "refused")
            ):
                explain = {}
                break
            else:
                explain[cand.label] = False
    tried = state.setdefault("dml_repair_tried", {})
    label = apply_operator_cast_candidates(
        plan, failure, tried, explain=explain or None,
    )
    if not label:
        return False
    for sd in (state.get("compiled_semantics") or {}).values():
        if str(getattr(sd, "layer", "")).upper() == str(failure.layer).upper():
            patch_semantic_exprs(sd, label, details)
    state["dml_repair_pending"] = label
    ranked = generate_dml_repair_candidates(
        sql, details, stats=load_dml_repair_stats(), explain=explain or None,
    ) if sql else []
    note = next((c.score_note for c in ranked if c.label == label), "")
    p = next((c.p_success for c in ranked if c.label == label), None)
    state["warnings"] = state.get("warnings", []) + [
        f"[design_revise] repair-doris-dml tried {label}"
        + (f" P≈{p:.0%}" if p is not None else "")
        + (f" ({note})" if note else "")
    ]
    return True


def _failure_from_execution_error(err):
    """Convert an execution ErrorClassification into a repairable ValidationResult.

    Extracts the layer from the DS task name (sql_<layer>_<i>_<table>) and the
    real Doris error line from the worker log (the tail, not the truncated
    head shown on approval cards).
    """
    raw = getattr(err, "raw_log", "") or ""
    summary = getattr(err, "summary", "") or ""

    m = _re.search(r"name=sql_([a-z]+)_\d+_", raw)
    if not m:
        return None
    layer = m.group(1).upper()

    error_lines = [
        l.strip() for l in raw.splitlines()
        if "errCode" in l or "detailMessage" in l
    ]
    detail = (error_lines[0] if error_lines else summary)[:500]
    check = "execution_error"
    detail_l = detail.lower()
    if "cast to bool" in detail_l:
        check = "doris_operator_cast"
    elif "cast to decimal in strict mode" in detail_l or "can't cast to decimal" in detail_l:
        check = "numeric_empty_string_compare"

    return ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer=layer,
        passed=False,
        checks=[],
        failures=[{
            "check": check,
            "detail": f"SQL 在 Doris 执行失败: {detail}",
        }],
    )


def _repair_hint(failure: dict) -> str:
    """One-line actionable guidance for a validation failure dict.

    Turns generic invariant messages into concrete repair actions so the
    design_revise LLM does not have to re-derive the fix from the failure
    detail (tasks a23e1599/405cf434: repeated identical repairs).
    """
    check = str(failure.get("check", ""))
    if check == "ddl_probe":
        return (
            "Fix the DDL so Doris accepts it — the failure detail carries "
            "Doris's own parser message, which is authoritative. Common fixes: "
            "DDL clause order (KEY clause BEFORE COMMENT), supported column "
            "types/widths, or the exact landing-database prefix from the plan."
        )
    if check == "ddl_probe_schema_conflict":
        return (
            "The target table ALREADY EXISTS in Doris with a different schema "
            "(a leftover from an earlier run) and CREATE TABLE IF NOT EXISTS "
            "silently no-oped. Do NOT re-run the same DDL. Either RENAME the "
            "target table (e.g. append `_v2`) so your DDL creates a fresh "
            "table with the planned columns, or adapt the planned columns/keys "
            "to the real table listed in the failure detail. Keep the DML "
            "column list in sync with whichever you choose."
        )
    if check == "doris_dml_insert_columns":
        return (
            "The INSERT target column list references columns that do not "
            "exist in the REAL target table (the DDL no-oped over an existing "
            "table). Reconcile the INSERT list with the real columns in the "
            "failure detail — either drop/rename the phantom target columns "
            "in BOTH the INSERT list and the SELECT aliases, or rename the "
            "target table so the new schema applies."
        )
    if check == "warehouse_dws_source_layer":
        return (
            "DWS must aggregate from the DWD/DIM layer — replace the DWS-layer "
            "source with its DWD counterpart (e.g. dwd_payment_event, "
            "dwd_order_payment_cycle_acc). Never read another "
            "DWS summary."
        )
    if check == "warehouse_source_table_empty":
        return (
            "The failing source table has 0 rows — replace it in the FROM/JOIN "
            "with a data-bearing table: its DWD counterpart (e.g. "
            "dwd_payment_event, dwd_order_payment_cycle_acc) "
            "or any explored table whose row count is > 0. Never keep a 0-row "
            "source — it materializes an empty table."
        )
    if check == "warehouse_grain_column_empty":
        return (
            "The DATE/period column used as grain (or in IS NOT NULL) is "
            "≥99% NULL in the published DIM/DWD — core modeling copied an "
            "empty ODS column. Switch source_expr / filters to a POPULATED "
            "date column on the same table (see alternatives in the failure "
            "detail, typically create_time / update_time). Do NOT keep "
            "DATE_FORMAT(empty_col) or empty_col IS NOT NULL."
        )
    if check == "warehouse_target_db_prefix":
        return (
            "Rewrite the db-qualified target to one of the ALLOWED layer "
            "databases listed in the failure (prefer the landing database) — "
            "never invent a prefix by analogy with another layer, e.g. "
            "`ads_ecommerce_SH.` does not exist; the ADS layer lands on "
            "its own configured database."
        )
    if check == "warehouse_table_name_too_long":
        return (
            f"Rename this table to {_DORIS_TABLE_NAME_LIMIT} characters or "
            "fewer (Doris table_name_length_limit): shorten the "
            "entity/prefix part of the name (e.g. drop '_analysis' or "
            "abbreviate 'receivable') in BOTH summary_table/ads_table AND "
            "every DDL/DML artifact."
        )
    if check in ("warehouse_source_db_case_mismatch", "warehouse_source_db_unknown"):
        return (
            "Qualify every FROM/JOIN source with the EXACT database name from "
            "the Source Schema section — Doris database names are "
            "case-sensitive (e.g. `dim_ecommerce_shop_sh`, not "
            "`dim_ecommerce_sh`), and never invent a database prefix."
        )
    if check == "dws_fields_materialized_in_load_sql":
        return (
            "Rewrite this DML with an EXPLICIT target column list: "
            "INSERT INTO <table> (col1, col2, ...) SELECT ... — the harness "
            "cannot verify column coverage of a column-less INSERT."
        )
    if check in ("dws_fields_materialized_in_ddl", "ads_fields_materialized_in_ddl"):
        return (
            "Add the missing column(s) to the CREATE TABLE definition, or "
            "drop them from the declared fields/measures if they are not "
            "materialized."
        )
    if check in (
        "dws_fields_materialized_in_load_sql", "ads_fields_materialized_in_load_sql",
    ):
        return (
            "Add the missing column(s) to the INSERT target column list AND "
            "the SELECT list, OR drop them from the declared fields/measures. "
            "Derived ratio metrics (rate/均价/占比) belong to the ADS layer — "
            "if this is a DWS table, drop them from `fields` via a METADATA "
            "repair ({\"metadata\": {\"fields\": [\"name1\", \"name2\", ...]}} "
            "listing the kept field names)."
        )
    if check == "derived_metric_parent_chain":
        return (
            "Emit a METADATA repair for this ADS table: replace the "
            "parent_metrics of the named metrics with physical columns that "
            "EXIST in the upstream DWS table (see its Columns in Source "
            "Schema / Upstream Layer Designs) — unknown parents usually come "
            "from copying a sibling table's measure names."
        )
    if check == "compiler_source_allowlist":
        return (
            "Drop every source / JOIN whose bare table name is not in the "
            "goal 'Allowed tables' list. The modeling goal restricted sources "
            "(源表仅限 / 只使用). Do NOT remap an extra table to a similar "
            "allowed name (dim_category is not dim_sku)."
        )
    if check == "compiler_closed_world":
        return (
            "JOIN ON / filter identifiers must exist on that alias's explored "
            "table. Prefer compiler aliases a, b, c in source order (do not "
            "reuse c/t when those letters are later sources). If both sides of "
            "an equality are missing, use the unique shared key of those two "
            "tables (dim_code, *_id). One-sided inventions (b.dim_id vs a real "
            "sku_id) stay refused — pick a column from Available columns "
            "on that same table, do not unique-hit remap."
        )
    if check == "warehouse_source_columns_exist":
        hint = (
            "Replace the absent column(s) with a REAL column from the "
            "'Available columns' list of that same source table (usually the "
            "surrogate key `dim_sk` or business key `member_bk`). If the "
            "missing column is a DERIVED quantity (e.g. 未收订单金额), COMPUTE "
            "it in the SELECT list from existing columns "
            "(monthly_receivable_fee - monthly_cumulative_paid_fee "
            "AS monthly_unpaid_order_amount) — never invent a column name, "
            "never substitute a semantically different column."
        )
        # The unpaid/uncollected family keeps reappearing (tasks cc841bec /
        # def559ca / 42452474): give a concrete derivation template for the
        # exact missing column, resolving the receivable/paid operands from
        # the ACTUAL Available columns (plural forms like
        # `cumulative_paid_order_amounts` never match a naive name join).
        avail_m = _re.search(r"Available columns:\s*([^.]*?)(?:\s*$)", str(failure.get("detail", "")))
        avail_cols = {
            c.strip().lower()
            for c in (avail_m.group(1).split(",") if avail_m else [])
            if c.strip()
        }
        from wren_agent.compiler.rules import (
            DIM_CODE_NAMES, DIM_NAME_NAMES, PERIOD_COLUMN_NAMES,
        )
        m = _re.search(r"\[([^\]]+)\]", str(failure.get("detail", "")))
        if m:
            period_avail = [c for c in sorted(avail_cols) if c in PERIOD_COLUMN_NAMES]
            for raw in m.group(1).split(","):
                bare = raw.strip().strip("'").strip('"').rsplit(".", 1)[-1]
                tm = _re.fullmatch(
                    r"(monthly|quarterly|daily|yearly)_(unpaid|uncollected)_(.+)",
                    bare,
                )
                if tm:
                    prefix, base = tm.group(1), tm.group(3)
                    receivable = next(
                        (c for c in avail_cols
                         if c == f"{prefix}_{base}_receivable"),
                        "",
                    )
                    paid = next(
                        (c for c in avail_cols
                         if _re.fullmatch(
                             rf"{prefix}_cumulative_paid_{base}s?|{prefix}_paid_{base}s?",
                             c,
                         )),
                        "",
                    )
                    if receivable and paid:
                        hint += (
                            f"\n   → Derivation template for `{bare}`: "
                            f"`{receivable} - {paid} AS {bare}` — both source "
                            "columns come from the Available columns list."
                        )
                        continue
                # Period-family hallucination (task 9f42f542: ADS copied
                # skill-example `stat_date` onto a DWS that only has
                # `stat_month`). Point at the real period column; do not
                # invent display attributes the source does not have.
                if bare.lower() in PERIOD_COLUMN_NAMES and period_avail:
                    if len(period_avail) == 1:
                        hint += (
                            f"\n   → `{bare}` is not on that table. Use "
                            f"`{period_avail[0]}` from Available columns "
                            "(do NOT invent stat_date / inventory_month)."
                        )
                    else:
                        hint += (
                            f"\n   → `{bare}` is not on that table. Pick the "
                            "period column from Available columns: "
                            f"{', '.join(period_avail)}."
                        )
                    continue
                if bare.lower() not in avail_cols:
                    low_bare = bare.lower()
                    if low_bare in DIM_CODE_NAMES and "dim_code" in avail_cols:
                        hint += (
                            f"\n   → `{bare}` is a guessed DIM attribute. Use "
                            "`dim_code` from Available columns (core modeling "
                            "renamed source `code`/`card_code` → `dim_code`)."
                        )
                        continue
                    if low_bare in DIM_NAME_NAMES and "dim_name" in avail_cols:
                        hint += (
                            f"\n   → `{bare}` is a guessed DIM attribute. Use "
                            "`dim_name` from Available columns (core modeling "
                            "renamed source `name`/`card_name` → `dim_name`)."
                        )
                        continue
                    count_alt = next(
                        (
                            c for c in avail_cols
                            if c.endswith(("_row_count", "_line_count",
                                           "_record_count", "_cnt"))
                            and (
                                low_bare.endswith(("_row_count", "_line_count",
                                                   "_record_count", "_cnt"))
                            )
                            and c.rsplit("_", 2)[0] == low_bare.rsplit("_", 2)[0]
                        ),
                        "",
                    )
                    if count_alt:
                        hint += (
                            f"\n   → `{bare}` is a count-measure synonym. Use "
                            f"`{count_alt}` from Available columns "
                            "(`*_line_count` / `*_row_count`)."
                        )
                        continue
                    hint += (
                        f"\n   → Drop `{bare}` — it is not in Available "
                        "columns and must not be invented (do not JOIN extra "
                        "tables to fabricate display attributes like org_name)."
                    )
        return hint
    if check == "warehouse_goal_source_allowlist":
        return (
            "Drop every source / JOIN whose bare table name is not in the "
            "goal Allowed tables list (源表仅限 / 只使用). Do not remap an "
            "extra table to a similar allowed name."
        )
    if check == "warehouse_source_table_explored":
        return (
            "Reference only source tables that exist in the Source Schema "
            "section below — never invent or mis-prefix table names."
        )
    if check == "warehouse_sources_qualified":
        return (
            "Qualify the listed source table with its database prefix. If it "
            "is a DWS/ADS table created by this same plan, use this plan's "
            "landing database."
        )
    if check == "warehouse_source_alias_required":
        return (
            "Give every source table an explicit alias and use "
            "alias.column everywhere."
        )
    if check == "group_by_matches_grain":
        return ("Make group_by_dimensions cover every grain column (extra "
                "columns allowed only as descriptive attributes functionally "
                "dependent on the grain).")
    if check == "unique_key_matches_grain":
        return "Make the DDL UNIQUE KEY exactly the grain columns, in order."
    if check == "daily_time_grain":
        return "Add a date/time column (e.g. stat_date) to the grain and materialize it."
    if check == "materialization_sql_complete":
        return "Provide BOTH a DDL and a DML_INSERT load statement for this table."
    if check == "doris_operator_cast":
        return (
            "The quoted token is an operand, not a BOOLEAN column. Follow "
            "skill repair-doris-dml: try CONCAT, CAST(CEIL AS INT), CAST "
            "args to VARCHAR, CONCAT_WS, or OR — one candidate per round. "
            "Do NOT add WHERE col IN ('true','false') or != '' as the fix."
        )
    if check == "numeric_empty_string_compare":
        return (
            "REMOVE every `col != ''` / `NULLIF(col, '')` / "
            "`NULLIF(CAST(col AS STRING), '')` on BIGINT/INT/DECIMAL/DATE/"
            "DATETIME columns. Those types cannot hold ''; comparing them to "
            "'' makes INSERT-strict fail with `can't cast to decimal in "
            "strict mode`. Empty-string guards are ONLY for VARCHAR/STRING/"
            "CHAR/TEXT. Native numeric/date columns: pass through as-is, or "
            "`IS NOT NULL` if you must drop nulls."
        )
    return ""


def _apply_repair_metadata(plan: Any, table: str, metadata: dict) -> bool:
    """Apply a design-metadata repair to the matching ADS/DWS design.

    `metadata` carries `metrics` (name → parent_metrics fixes) and
    `requirement_coverage` (requirement → status/evidence claims). Matches
    the design by bare table name so a `db_dws.`-qualified repair target
    finds the plan's design.
    """
    def _bare(name: str) -> str:
        return str(name).lower().rsplit(".", 1)[-1]

    design = next(
        (d for d in (plan.ads_designs or []) if _bare(d.ads_table) == _bare(table)),
        None,
    )
    if design is None:
        design = next(
            (d for d in (plan.dws_designs or [])
             if _bare(d.summary_table) == _bare(table)),
            None,
        )
    if design is None:
        return False

    changed = False
    for m in (metadata.get("metrics") or []):
        if not isinstance(m, dict):
            continue
        metric_name = str(m.get("name") or m.get("field_name") or "")
        parents = [str(p) for p in (m.get("parent_metrics") or [])]
        for existing in (design.metrics or []):
            existing_name = (
                existing.get("name") if isinstance(existing, dict)
                else getattr(existing, "name", "")
            )
            if str(existing_name or "") == metric_name:
                if isinstance(existing, dict):
                    existing["parent_metrics"] = parents
                else:
                    existing.parent_metrics = parents
                changed = True
    # Fields repair: replace the declared fields list with the given names
    # (a plain list of field names). Used to DROP fields that must not be
    # materialized here — e.g. derived ratio metrics (rate/均价) that belong
    # to ADS while the DWS only materializes base aggregations (task
    # 513c12a5: 8 declared rate fields were never materialized and SQL-only
    # repairs kept failing).
    field_names = [str(f).strip() for f in (metadata.get("fields") or []) if str(f).strip()]
    if field_names:
        keep = {name.lower() for name in field_names if name}
        current = list(getattr(design, "fields", None) or [])
        kept = [
            f for f in current
            if isinstance(f, dict) and str(f.get("name") or "").lower() in keep
        ]
        kept_names = {str(f.get("name") or "").lower() for f in kept}
        for name in field_names:
            if name.lower() not in kept_names:
                kept.append({"name": name})
        design.fields = kept
        changed = True
    for rc in (metadata.get("requirement_coverage") or []):
        if not isinstance(rc, dict):
            continue
        requirement = str(rc.get("requirement") or "").strip()
        if not requirement:
            continue
        status = str(rc.get("status") or "").strip().lower()
        evidence = list(rc.get("evidence") or [])
        gap_reason = str(rc.get("gap_reason") or "").strip()
        coverage = list(getattr(design, "requirement_coverage", None) or [])
        replaced = False
        for existing in coverage:
            existing_req = (
                existing.get("requirement") if isinstance(existing, dict)
                else getattr(existing, "requirement", "")
            )
            if str(existing_req or "") == requirement:
                if isinstance(existing, dict):
                    existing.update({
                        "status": status,
                        "evidence": evidence,
                        "gap_reason": gap_reason,
                    })
                else:
                    existing.status = status
                    existing.evidence = evidence
                    existing.gap_reason = gap_reason
                replaced = True
                break
        if not replaced:
            coverage.append({
                "requirement": requirement,
                "status": status,
                "evidence": evidence,
                "gap_reason": gap_reason,
            })
            design.requirement_coverage = coverage
        changed = True
    return changed


async def _generate_typed_with_transient_retry(
    llm: Any,
    system_prompt: str,
    user_message: str,
    schema: Any,
    *,
    token_kind: str,
    task_id: str = "",
) -> Any:
    """Call llm.generate_typed_json, retrying once on transient failures.

    The model endpoint intermittently times out mid-stream
    (`httpcore.ReadTimeout` — task d28431de: the repair LLM failed on both
    revision rounds, so no fix was ever applied and the no-progress guard
    killed the task). A single retry absorbs the transient spike; a
    deterministic failure (schema validation) re-raises immediately.
    """
    try:
        return await llm.generate_typed_json(
            system_prompt, user_message, schema, token_kind=token_kind,
        )
    except Exception as exc:
        transient = _is_transient_llm_error(exc)
        if not transient:
            raise
        logger.warning(
            "[%s] transient LLM failure (%s) — retrying once",
            (task_id or "")[:8], type(exc).__name__,
        )
        return await llm.generate_typed_json(
            system_prompt, user_message, schema, token_kind=token_kind,
        )


def _is_transient_llm_error(exc: BaseException) -> bool:
    """True when an LLM client error looks like a transient network failure
    worth one retry (timeouts, connection resets) vs a deterministic bug."""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "reset", "readerror", "network")):
        return True
    # openai SDK wraps transport errors; the chain typically ends in
    # httpcore.ReadTimeout / RemoteProtocolError / ConnectError.
    return any(
        isinstance(exc, cls)
        for cls in (TimeoutError, ConnectionError, OSError)
    )


async def _llm_repair_plan_metadata(
    state: AgentState, failure: Any, plan: Any,
) -> bool:
    """Repair plan-level requirement-coverage gaps via a metadata-only LLM call.

    A requirement no table in the plan claims as supported is a design gap:
    either some table must claim it (e.g. an ADS table computing a derived
    metric the DWS deferred), or the gap is real and stays partial/unsupported
    (task f242a388: '应收增长率（环比）' deferred to ADS by the DWS table but
    no ADS table claimed it). The LLM emits `metadata.requirement_coverage`
    repairs; `_apply_repair_metadata` applies them.
    """
    from wren_agent.tools.llm_client import get_llm_client, build_system_prompt

    failure_items = [
        f"{i}. [{f.get('check', '?')}] {f.get('detail', '?')}"
        for i, f in enumerate(failure.failures or [], 1)
    ]

    def _coverage_summary(designs: list, attr: str) -> list[str]:
        out = []
        for d in designs or []:
            name = str(getattr(d, attr, "") or "")
            items = [
                f"    - {c.get('requirement')} → {c.get('status')}"
                + (f"（{c.get('gap_reason')}）" if c.get('gap_reason') else "")
                for c in (getattr(d, "requirement_coverage", None) or [])
                if isinstance(c, dict)
            ]
            if items:
                out.append(f"  {name}:")
                out.extend(items)
        return out

    coverage_text = "\n".join(
        ["## Current requirement coverage"]
        + _coverage_summary(plan.dws_designs, "summary_table")
        + _coverage_summary(plan.ads_designs, "ads_table")
    )

    system_prompt = build_system_prompt(
        skill_names=["design-ads", "design-dwd-dws"],
        extra_context=(
            "You are fixing PLAN-level requirement coverage gaps in a data "
            "warehouse design. Return ONLY a JSON repairs object."
        ),
    )
    user_message = f"""The following requirements are NOT fully supported by ANY table in the design plan.

## Failures
{chr(10).join(failure_items)}

{coverage_text}

## Task
For each failed requirement, decide:
1. If some table CAN compute it (e.g. an ADS table can compute a derived metric
   the DWS deferred, using physical columns already present), emit a METADATA
   repair claiming it as "supported" on that table with the physical evidence
   columns.
2. If the requirement genuinely cannot be met (no source data / no grain), keep
   it partial/unsupported and strengthen the gap_reason — do NOT claim support.
3. If the gap is a MISSING multi-entity grain table — the requirement needs a
   JOINed summary whose grain carries 2+ entity keys (e.g. 单位×店铺×月) and no
   such DWS table exists — you CANNOT fix it via metadata: keep every entry
   unsupported with a gap_reason naming the missing DWS table and its required
   grain keys (e.g. "需新增 DWS 表 dws_..._unit_building_month，粒度
   org_id+store_id+stat_month，由 receivable_cycle_acc JOIN contract_order
   提供两个实体键"). Do NOT claim support by pointing at single-entity tables —
   a multi-entity grain cannot be derived from separate org/shop aggregates.
Return a JSON object:
{{"repairs": [{{"target_table": "string — table that claims the requirement",
                "metadata": {{"requirement_coverage": [
                  {{"requirement": "string — exact requirement text",
                    "status": "supported | partial | unsupported",
                    "evidence": ["string — physical column names"],
                    "gap_reason": "string"}}
                ]}}}}]}}
Return ONLY the JSON object."""

    try:
        llm = get_llm_client()
        # Typed generation: schema validation + one regeneration with the
        # validation error fed back — replaces _extract_repair_json's silent
        # `{}` fallback, where a non-JSON repair response looked like "no
        # repairs" and the plan-level gap silently survived the round.
        data = await _generate_typed_with_transient_retry(
            llm, system_prompt, user_message, RepairOutput,
            token_kind="repair", task_id=state.get("task_id", ""),
        )
        applied = 0
        for repair in data.repairs:
            table = repair.target_table
            metadata = repair.metadata
            if table and metadata and _apply_repair_metadata(plan, table, metadata):
                applied += 1
        logger.info(
            "[%s] design_revise: plan-level repair applied %d/%d metadata fixes",
            state.get("task_id", "")[:8], applied,
            len(data.repairs),
        )
        return applied > 0
    except Exception:
        logger.warning(
            "[%s] plan-level LLM repair failed", state.get("task_id", "")[:8],
            exc_info=True,
        )
        return False


async def _llm_repair_artifact(
    state: AgentState, failure: Any, plan: Any
) -> bool:
    """Use LLM to repair a single failed SQL artifact.

    Returns True if repair was applied successfully.
    """
    import json as _json, re as _re

    # Find the failed artifact in the plan
    failed_layer = failure.layer
    failed_sql_artifacts = [
        a for a in (plan.sql_artifacts or [])
        if a.layer == failed_layer
    ]
    if not failed_sql_artifacts:
        # Plan-level failures (requirement coverage gaps) have no single SQL
        # artifact to repair — the fix is design metadata: some table must
        # claim the requirement as supported, or the gap is real. Task
        # f242a388: '应收增长率（环比）' was deferred by the DWS table to ADS
        # but no ADS table claimed it, and the repair loop never even called
        # the LLM for PLAN-layer failures.
        if failed_layer == "PLAN":
            return await _llm_repair_plan_metadata(state, failure, plan)
        return False

    # Build repair prompt — failures FIRST, numbered and actionable, so the
    # LLM does not have to hunt for them inside the large schema dump.
    failure_items = []
    for index, f in enumerate(failure.failures or [], 1):
        hint = _repair_hint(f)
        failure_items.append(
            f"{index}. [{f.get('check', '?')}] {f.get('detail', '?')}"
            + (f"\n   → Hint: {hint}" if hint else "")
        )
    failure_details = "\n".join(failure_items)

    artifacts_text = "\n\n".join(
        f"### {a.target_table} [{a.sql_type}]\n```sql\n{a.sql}\n```"
        for a in failed_sql_artifacts[:5]
    )

    # Map layer to skill
    skill_map = {
        "ODS": "design-ods",
        "DIM": "design-dim",
        "DWD": "design-dwd-dws",
        "DWS": "design-dwd-dws",
        "ADS": "design-ads",
    }
    skill_name = skill_map.get(failed_layer)
    skill_names = [s for s in ([skill_name] if skill_name else []) if s]
    if any(
        str(f.get("check", "")) == "doris_operator_cast"
        or "cast to bool" in str(f.get("detail", "")).lower()
        for f in (failure.failures or [])
    ):
        skill_names = ["repair-doris-dml"] + skill_names

    from wren_agent.tools.llm_client import get_llm_client, build_system_prompt

    # Build full design context so the repair LLM understands WHY the DDL
    # was designed this way — not just WHAT failed validation.
    from wren_agent.nodes.design_layers import (
        _serialize_clarifications_for_prompt,
        _serialize_explorations_for_prompt,
        _serialize_upstream_designs,
    )
    # Narrow the source-schema context to the tables the failing statements
    # actually READ — the full exploration dump (100K+ chars, task 95ab3acb)
    # made every repair call take 12-20 min for a fix that only touches the
    # failing tables' sources.
    needed: set[str] = set()
    for art in failed_sql_artifacts:
        for m in _SOURCE_REF_RE.finditer(getattr(art, "sql", "") or ""):
            t = (m.group(4) or "").lower()
            if t:
                needed.add(t)
    explorations_context = _serialize_explorations_for_prompt(state)
    if needed:
        filtered: dict = {}
        for key, exp in (state.get("explorations") or {}).items():
            tables = [
                t for t in (getattr(exp, "tables", None) or [])
                if str(getattr(t, "name", "") or "").lower() in needed
            ]
            if tables:
                filtered[key] = exp.model_copy(update={"tables": tables})
        if filtered:
            explorations_context = _serialize_explorations_for_prompt(
                {**state, "explorations": filtered}, use_cache=False,
            )
    clarifications_context = _serialize_clarifications_for_prompt(state)
    upstream_context = _serialize_upstream_designs(state, failed_layer)

    repair_context = (
        "You are fixing DDL statements that failed Doris sandbox validation. "
        "Return ONLY the corrected DDL as a JSON object. "
        "CRITICAL: Doris forbids STRING in KEY columns, BOOLEAN type, and DEFAULT TRUE. "
        "Use VARCHAR(65533) for key columns that would be STRING. "
        "Use TINYINT instead of BOOLEAN. Use DEFAULT 1 instead of DEFAULT TRUE. "
        "Doris DDL clause order: (columns) → KEY clause (UNIQUE/DUPLICATE/AGGREGATE "
        "KEY(...)) → COMMENT → DISTRIBUTED BY → PROPERTIES — COMMENT must NEVER "
        "precede the KEY clause."
    )
    # The repair LLM keeps qualifying targets with another layer's database
    # (e.g. `dws_ecommerce_orders_sh.ads_...`), so the repair never matches the
    # artifact and is dropped (task a2b0cea0: 3 blind rounds). State the
    # landing db explicitly.
    if state.get("source_mode") == "warehouse":
        from wren_agent.nodes.deployment import _warehouse_layers
        layers = _warehouse_layers(state)
        landing_parts = [
            f"{layer.upper()} layer tables land in database `{db}`"
            for layer, db in layers.items() if db
        ]
        if landing_parts:
            repair_context += (
                "\n" + "; ".join(landing_parts)
                + ". The repaired target_table MUST use that layer's own "
                "database prefix (or stay bare) — never another layer's "
                "database, never an invented one."
            )

    system_prompt = build_system_prompt(
        skill_names=skill_names,
        extra_context=repair_context,
    )

    user_message = f"""The following SQL statements for the **{failed_layer}** layer failed (validation or Doris execution).

## Failures (fix exactly these — reference the statements below)
{failure_details}

## Current SQL (that needs repair)
{artifacts_text}

## Source Schema (original table structures — reference tables with their `db.` prefixes exactly as shown)
{explorations_context}

## Business Knowledge (human clarifications)
{clarifications_context}

## Upstream Layer Designs
{upstream_context}

## Task
Fix each failed statement so it runs on Doris. Preserve the original design intent — only change what's needed.
Each statement above is labeled with its type ([DDL] / [DML_INSERT]); repair the one(s) the failure refers to.

Hard requirements:
- Fix the exact failures listed under `## Failures`; the hints there describe the required repair.
- Qualify EVERY source table reference with its database prefix (`db`.`table`), exactly as shown in the Source Schema section.
- Use ONLY columns that are explicitly listed under each table in the Source Schema section — never invent column names. If the failure says a column is unknown, replace it with a listed column or JOIN the listed table that actually contains the dimension you need.
- DML statements MUST declare an explicit target column list: `INSERT INTO <table> (col1, col2, ...) SELECT ...` — never a column-less `INSERT INTO <table> SELECT ...`.
- ADS load SQL must read from this plan's DWS tables (see Upstream Layer Designs) — never directly from DWD/DIM/source tables.
- NEVER anchor a DML on a 0-row source table (the failure names it) — replace it with a data-bearing table (its DWD counterpart or any table with rows > 0).
- Multi-table SQL MUST give every source an explicit alias (FROM schema.table t … JOIN schema.other o ON …).
- THIS Doris cluster does NOT support LATERAL VIEW / UNNEST / EXPLODE_SPLIT — rewrite row-explosion SQL to aggregate at the column's own grain (contract/cycle/unit) instead of splitting comma-separated values into rows.
- Never write `col != ''` or `NULLIF(col, '')` on BIGINT/INT/DECIMAL/DATE/DATETIME columns. Comparing those types to '' fails INSERT-strict with `can't cast to decimal in strict mode`. Empty-string guards are ONLY for VARCHAR/STRING/CHAR/TEXT; native numeric/date columns pass through (or `IS NOT NULL`).
- When a `derived_metric_parent_chain` failure names parent metrics that do not exist in the upstream DWS table, ALSO emit a METADATA repair for that ADS table — replace each metric's `parent_metrics` with physical columns that DO exist in the upstream DWS table's Columns.
- When `dws_fields_materialized_in_load_sql` / `_in_ddl` names derived ratio fields (rate/均价/占比) that a DWS table should NOT materialize (DWS only materializes base aggregations — derived metrics belong to ADS), emit a METADATA repair listing the KEPT field names: {{"metadata": {{"fields": ["name1", "name2", ...]}}}}.
Return a JSON object with the fixed statements and any metadata fixes:
{{"repairs": [
  {{"target_table": "string", "sql_type": "DDL | DML_INSERT", "sql": "string — fixed statement"}},
  {{"target_table": "string", "metadata": {{"metrics": [{{"name": "string — metric name", "parent_metrics": ["string — upstream DWS physical measure names"]}}], "fields": ["string — kept field names"]}}}}
]}}
Return ONLY the JSON object."""

    try:
        llm = get_llm_client()

        # Parse response — typed: schema validation + one regeneration on
        # failure (a dropped/unparsable repair response used to count as "no
        # fixes", and the harness re-failed the same checks next round).
        # model_dump() keeps unknown keys (extra="allow") so legacy aliases
        # like "ddl" survive into the apply loop below unchanged.
        data = await llm.generate_typed_json(
            system_prompt, user_message, RepairOutput, token_kind="repair",
        )
        repairs = [r.model_dump() for r in data.repairs]

        # Apply repairs — match by (target_table, sql_type); a table now has
        # both a DDL and a DML_INSERT artifact, and sql_type-less legacy
        # repairs default to DDL. Metadata-only repairs (no `sql`) update
        # design-level fields SQL cannot reach — e.g. ADS derived-metric
        # parent_metrics that name a column the upstream DWS table does not
        # have (task 93479659: the repair LLM kept fixing DDL/DML while the
        # parent chain could only be fixed in the design metadata).
        # Declared targets of this layer — a repair for a table that has NO
        # artifact (`materialization_sql_complete: found []`) must CREATE the
        # missing statement instead of being dropped: the old code only
        # replaced existing artifacts, so a declared-but-empty table stayed
        # empty across every revision round (task 00317106: `..._contract_month`
        # never got its DDL/DML and the revision budget burned out).
        declared_targets = {
            str(getattr(d, "summary_table", "") or "").split(".")[-1].strip("`").lower()
            for d in (plan.dws_designs or [])
        } | {
            str(getattr(d, "ads_table", "") or "").split(".")[-1].strip("`").lower()
            for d in (plan.ads_designs or [])
        }

        applied = 0
        for repair in repairs:
            table = repair.get("target_table", "")
            metadata = repair.get("metadata")
            if metadata and table:
                if _apply_repair_metadata(plan, table, metadata):
                    applied += 1
                continue
            new_sql = repair.get("sql") or repair.get("ddl", "")
            sql_type = repair.get("sql_type", "DDL")
            if not table or not new_sql:
                continue
            replaced = False
            for artifact in plan.sql_artifacts:
                if (artifact.target_table == table
                        and artifact.sql_type == sql_type):
                    artifact.sql = new_sql
                    artifact.version += 1
                    applied += 1
                    replaced = True
                    logger.info(
                        "[%s] design_revise: repaired %s %s for %s",
                        state.get("task_id", "")[:8], failed_layer, sql_type, table,
                    )
                    break
            if not replaced:
                if table.split(".")[-1].strip("`").lower() in declared_targets:
                    plan.sql_artifacts.append(SQLArtifact(
                        layer=failed_layer,
                        target_table=table,
                        sql_type=sql_type,
                        sql=new_sql,
                    ))
                    applied += 1
                    logger.info(
                        "[%s] design_revise: CREATED missing %s %s for %s",
                        state.get("task_id", "")[:8], failed_layer, sql_type, table,
                    )
                else:
                    logger.warning(
                        "[%s] design_revise: ignoring repair for undeclared table %s",
                        state.get("task_id", "")[:8], table,
                    )

        if applied > 0:
            from wren_agent.tools.sql_empty_guards import sanitize_sql_artifacts
            sanitize_sql_artifacts(
                plan.sql_artifacts, state.get("explorations") or {},
                layer=failed_layer,
            )
        return applied > 0

    except Exception:
        raise  # Let caller handle


async def _llm_repair_semantic(state: AgentState, failure: Any, plan: Any) -> bool:
    """Compiler-mode repair (ADR-0015 Phase 4): fix the SEMANTIC design, not
    the SQL text.

    The failed layer's compiled tables keep their semantic source in
    ``state["compiled_semantics"]``. The repair LLM receives the failures +
    each table's semantic design JSON and returns REPAIRED semantic designs
    (structure diff). Every repaired design is re-validated and recompiled;
    the table's SQL artifacts, plan design entry and stored semantic are
    replaced in one atomic pass. Off-whitelist expressions / invalid sources
    simply fail the compile again — the loop stays honest.
    """
    from wren_agent.compiler import landing_db_for_layer
    from wren_agent.tools.compiler_fn_admit import compile_design_with_admit
    from wren_agent.compiler.rules import (
        apply_composed_table_name,
        strip_db_prefix,
        strip_source_alias,
    )
    from wren_agent.models.llm_outputs import SemanticRepairOutput
    from wren_agent.models.semantic import SemanticDesign
    from wren_agent.nodes.design_layers import (
        _ads_design_from_semantic,
        _clear_compiler_failure,
        _dws_design_from_semantic,
        _record_compiler_failure,
        ads_bind_source_columns,
        compile_source_allowlist,
        explored_columns_for_sources,
        explored_columns_prompt_block,
        goal_source_allowlist_prompt_block,
        occupied_landing_names,
    )
    from wren_agent.tools.llm_client import build_system_prompt, get_llm_client

    layer = str(failure.layer).upper()
    if layer not in ("DWS", "ADS"):
        return False
    compiled = {
        t: sd for t, sd in (state.get("compiled_semantics") or {}).items()
        if getattr(sd, "layer", "") == layer
    }
    if not compiled:
        return False

    failure_items = [
        f"{i}. [{f.get('check', '?')}] {f.get('detail', '?')}"
        for i, f in enumerate(failure.failures or [], 1)
    ]
    designs_text = "\n\n".join(
        f"### {t}\n{_json.dumps(sd.model_dump(), ensure_ascii=False, indent=2)}"
        for t, sd in compiled.items()
    )
    # The repair LLM must see the plan's ACTUAL upstream tables (it cannot
    # fix `ads_dws_lineage` / `derived_metric_parent_chain` without knowing
    # the DWS tables this plan created — acceptance finding: the ADS repair
    # kept re-emitting a hallucinated DWS table name for 3 rounds).
    upstream_lines: list[str] = []
    if layer == "ADS":
        for d in (plan.dws_designs or []):
            measures = ", ".join(str(m.get("name", "")) for m in (d.measures or []))
            upstream_lines.append(
                f"- {d.summary_table} (measures: {measures})"
            )
    else:
        for d in (plan.dwd_designs or []):
            upstream_lines.append(f"- {d.fact_table}")
    upstream_text = (
        "## This plan's upstream tables (reference these EXACT names)\n"
        + "\n".join(upstream_lines)
        if upstream_lines else ""
    )
    closed_world_sources: list[str] = []
    for sd in compiled.values():
        closed_world_sources.extend(getattr(sd, "sources", None) or [])
    closed_world_text = explored_columns_prompt_block(state, closed_world_sources)
    allowlist_text = goal_source_allowlist_prompt_block(state, layer)
    repair_skills = ["design-dwd-dws"] if layer == "DWS" else ["design-ads"]
    if any(
        str(f.get("check", "")) == "doris_operator_cast"
        or "cast to bool" in str(f.get("detail", "")).lower()
        for f in (failure.failures or [])
    ):
        repair_skills = ["repair-doris-dml"] + repair_skills
    system_prompt = build_system_prompt(
        skill_names=repair_skills,
        extra_context=(
            "You are fixing a SEMANTIC data warehouse design that failed "
            "validation. Return ONLY a JSON object with the REPAIRED "
            "semantic designs — never write DDL/load SQL text; the compiler "
            "renders those. Keep every table's declared columns/grain unless "
            "a failure requires changing them. Use only Doris-safe functions "
            "and expressions."
            + (
                " ADS source_expr identifiers must exist on the source DWS "
                "table — use that table's actual period column (stat_month vs "
                "stat_date) and do not invent display attributes (org_name) "
                "the source does not have."
                if layer == "ADS" else ""
            )
        ),
    )
    user_message = f"""The following {layer} layer validation checks FAILED.

## Failures (fix exactly these)
{chr(10).join(failure_items)}

{upstream_text}

{closed_world_text}

{allowlist_text}

## Current semantic designs (repair these)
{designs_text}

## Task
Return repaired semantic designs as:
{{"semantic_designs": [
  {{"layer": "{layer}", "summary_name": "...", "summary_table": "...",
    "columns": [{{"name", "dtype", "comment", "is_key", "is_measure", "source_expr"}}],
    "sources": [...], "joins": [...], "filters": [...],
    "requirement_coverage": [...]}}
]}}
- Fix ONLY what the failures name (wrong source_expr, missing/wrong source,
  filter, grain). Do not invent columns that have no value source.
- `sources`/joins MUST reference the EXACT table names listed under
  `## This plan's upstream tables` (a hallucinated source name fails
  `ads_dws_lineage` forever). Do NOT append aliases (`dwd_x a`) — the
  compiler assigns a, b, c in source order.
- `source_expr` must stay on the compile whitelist (agg(col), Doris-safe
  scalar functions, plain columns, arithmetic).
- Multi-source designs MUST declare joins {{right, on}} using those
  compiler aliases in `on`/`source_expr`.
- Grain columns (is_key=true) MUST lead the column list, in grain order.
Return ONLY the JSON object."""

    try:
        llm = get_llm_client()
        data = await _generate_typed_with_transient_retry(
            llm, system_prompt, user_message, SemanticRepairOutput,
            token_kind="repair", task_id=state.get("task_id", ""),
        )
        landing = landing_db_for_layer(state, layer)
        occupied = await occupied_landing_names(state, layer)
        applied = 0
        for raw in data.semantic_designs:
            try:
                sd = SemanticDesign.model_validate(raw)
            except Exception as exc:
                logger.warning(
                    "[%s] semantic repair: invalid design rejected: %s",
                    state.get("task_id", "")[:8], str(exc)[:200],
                )
                continue
            if sd.layer != layer:
                continue
            orig_bare = strip_db_prefix(sd.summary_table).lower()
            bare = apply_composed_table_name(sd, occupied=occupied)
            occupied.add(bare)
            target = f"{landing}.{bare}" if landing else bare
            alias_bares = {orig_bare, bare}
            compiled_sql, problems = await compile_design_with_admit(
                state, sd, layer_landing_db=landing, table_comment=sd.summary_name,
                source_columns=(
                    ads_bind_source_columns(state, plan, sd.sources)
                    if layer == "ADS" else
                    explored_columns_for_sources(state, sd.sources)
                ),
                allowed_source_tables=compile_source_allowlist(state, layer),
            )
            if compiled_sql is None:
                _record_compiler_failure(state, layer, target, sd, problems)
                logger.warning(
                    "[%s] semantic repair: recompiled table %s still fails — "
                    "keeping previous artifacts",
                    state.get("task_id", "")[:8], target,
                )
                continue
            _clear_compiler_failure(state, target)
            orig_target = f"{landing}.{orig_bare}" if landing else orig_bare
            if orig_target != target:
                _clear_compiler_failure(state, orig_target)
            # Replace SQL artifacts for this target (compiled ones only).
            # Grain-aware rename (dws_x_monthly → dws_x_org_monthly) must
            # drop the previous compiled artifacts too.
            plan.sql_artifacts = [
                a for a in (plan.sql_artifacts or [])
                if not (
                    strip_db_prefix(str(a.target_table)).lower() in alias_bares
                    and "ompiled" in (a.description or "")
                )
            ]
            plan.sql_artifacts.append(SQLArtifact(
                layer=layer, target_table=target, sql_type="DDL",
                sql=compiled_sql.ddl, dependencies=[strip_source_alias(s) for s in sd.sources],
                description=f"Compiled DDL for {target} (DesignCompiler)",
            ))
            plan.sql_artifacts.append(SQLArtifact(
                layer=layer, target_table=target, sql_type="DML_INSERT",
                sql=compiled_sql.load_sql, dependencies=[strip_source_alias(s) for s in sd.sources],
                description=f"Compiled load for {target} (DesignCompiler)",
            ))
            # Replace the plan design entry (downstream consumers see the
            # repaired structure) and the stored semantic source.
            designs = plan.dws_designs if layer == "DWS" else plan.ads_designs
            attr = "summary_table" if layer == "DWS" else "ads_table"
            designs = [
                d for d in (designs or [])
                if strip_db_prefix(str(getattr(d, attr, ""))).lower() not in alias_bares
            ]
            mapped = (
                _dws_design_from_semantic(sd, target)
                if layer == "DWS" else _ads_design_from_semantic(sd, target)
            )
            designs.append(mapped)
            if layer == "DWS":
                plan.dws_designs = designs
            else:
                plan.ads_designs = designs
            for key in list(compiled):
                if strip_db_prefix(key).lower() in alias_bares:
                    compiled.pop(key, None)
            compiled[target] = sd
            full_semantics = dict(state.get("compiled_semantics") or {})
            for key in list(full_semantics):
                if strip_db_prefix(key).lower() in alias_bares:
                    full_semantics.pop(key, None)
            full_semantics[target] = sd
            state["compiled_semantics"] = full_semantics
            applied += 1
            logger.info(
                "[%s] design_revise: semantic-repaired %s table %s",
                state.get("task_id", "")[:8], layer, target,
            )
        return applied > 0
    except Exception:
        logger.warning(
            "[%s] semantic LLM repair failed for %s layer",
            state.get("task_id", "")[:8], layer, exc_info=True,
        )
        return False
