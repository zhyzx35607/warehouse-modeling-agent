"""Deterministic ODS table checks and stage-quality aggregation."""

from __future__ import annotations

from wren_agent.models.stage import (
    ODSManifest, ODSQualityGate,
    ODSSyncMode,
    ODSTableRun,
    ODSTableStatus,
    StageStatus,
)


_RETRYABLE_ERROR_TYPES = {"transient"}


def evaluate_ods_quality(
    manifest: ODSManifest,
    runs: list[ODSTableRun],
    max_attempts: int = 3,
) -> ODSQualityGate:
    """Evaluate the latest attempt for every manifest table."""
    latest: dict[str, ODSTableRun] = {}
    for run in runs:
        if run.table_identity not in latest or run.attempt > latest[run.table_identity].attempt:
            latest[run.table_identity] = run

    manifest_tables = {table.table_identity: table for table in manifest.tables}
    unexpected = sorted(set(latest) - set(manifest_tables))
    if unexpected:
        raise ValueError(f"runs contain tables outside manifest: {unexpected}")

    passed: list[str] = []
    required_failures: list[str] = []
    optional_failures: list[str] = []
    retryable: list[str] = []
    checks: list[dict] = []

    for table_id, table in sorted(manifest_tables.items()):
        run = latest.get(table_id)
        if run is None:
            checks.append({
                "table_identity": table_id,
                "sync_mode": table.initial_mode.value,
                "passed": False,
                "reasons": ["execution_missing"],
                "attempt": 0,
            })
            (required_failures if table.required else optional_failures).append(table_id)
            continue
        reasons: list[str] = []
        if run.status != ODSTableStatus.SUCCEEDED:
            reasons.append(run.error or "execution_failed")
        elif run.source_rows is None or run.target_rows is None:
            reasons.append("row_counts_missing")
        elif run.source_rows != run.target_rows:
            check = "full_row_count_mismatch" if run.sync_mode == ODSSyncMode.FULL else "batch_row_count_mismatch"
            reasons.append(check)

        if run.sync_mode == ODSSyncMode.INCREMENTAL:
            if run.watermark_monotonic is not True:
                reasons.append("watermark_not_monotonic")
            if not run.watermark_committed:
                reasons.append("watermark_not_committed")

        table_passed = not reasons
        checks.append({
            "table_identity": table_id,
            "sync_mode": run.sync_mode.value,
            "passed": table_passed,
            "reasons": reasons,
            "attempt": run.attempt,
        })
        if table_passed:
            passed.append(table_id)
            continue
        if run.error_type in _RETRYABLE_ERROR_TYPES and run.attempt < max_attempts:
            retryable.append(table_id)
        if run.required:
            required_failures.append(table_id)
        else:
            optional_failures.append(table_id)

    if required_failures:
        status = StageStatus.FAILED
    elif optional_failures:
        status = StageStatus.PARTIAL_SUCCESS
    else:
        status = StageStatus.SUCCEEDED
    return ODSQualityGate(
        status=status,
        passed_tables=passed,
        failed_required_tables=required_failures,
        failed_optional_tables=optional_failures,
        retryable_tables=retryable,
        checks=checks,
    )
