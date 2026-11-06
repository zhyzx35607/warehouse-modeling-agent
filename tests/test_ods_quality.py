from wren_agent.models.stage import (
    ODSManifest, ODSQualityGate, ODSSyncMode, ODSTableManifest, ODSTableRun,
    ODSTableStatus, StageStatus,
)
from wren_agent.tools.ods_quality import evaluate_ods_quality


def _run(table="db:t", *, required=False, mode=ODSSyncMode.FULL, **kwargs):
    values = dict(task_id="task-1", manifest_version=1, table_identity=table,
                  attempt=1, required=required, sync_mode=mode,
                  status=ODSTableStatus.SUCCEEDED, source_rows=10, target_rows=10,
                  watermark_monotonic=True, watermark_committed=True)
    values.update(kwargs)
    return ODSTableRun(**values)


def _manifest(table="db:t", *, required=False, incremental=False):
    return ODSManifest(source_catalog_version=1, tables=[ODSTableManifest(
        table_identity=table, source_db_id="db", source_table="t", ods_table="ods_t",
        required=required, source_catalog_version=1,
        steady_mode=ODSSyncMode.INCREMENTAL if incremental else ODSSyncMode.FULL,
        incremental_column="updated_at" if incremental else "",
    )])


def test_full_requires_exact_row_count():
    gate = evaluate_ods_quality(_manifest(), [_run(source_rows=10, target_rows=9)])
    assert gate.status == StageStatus.PARTIAL_SUCCESS
    assert gate.checks[0]["reasons"] == ["full_row_count_mismatch"]


def test_incremental_checks_batch_count_and_watermark():
    gate = evaluate_ods_quality(_manifest(incremental=True), [
        _run(mode=ODSSyncMode.INCREMENTAL, target_rows=9, watermark_monotonic=False)])
    assert gate.status == StageStatus.PARTIAL_SUCCESS
    assert set(gate.checks[0]["reasons"]) == {
        "batch_row_count_mismatch", "watermark_not_monotonic",
    }


def test_required_failure_blocks_stage_and_transient_can_retry():
    gate = evaluate_ods_quality(_manifest(required=True), [
        _run(required=True, status=ODSTableStatus.FAILED,
             error_type="transient", error="timeout")])
    assert gate.status == StageStatus.FAILED
    assert gate.failed_required_tables == ["db:t"]
    assert gate.retryable_tables == ["db:t"]


def test_latest_attempt_wins_and_optional_failure_is_partial():
    first = _run(table="db:optional", attempt=1, status=ODSTableStatus.FAILED,
                 error_type="transient", error="timeout")
    second = _run(table="db:optional", attempt=2)
    gate = evaluate_ods_quality(_manifest("db:optional"), [first, second])
    assert gate.status == StageStatus.SUCCEEDED
    assert gate.passed_tables == ["db:optional"]


def test_manifest_defaults_to_periodic_full_without_cursor():
    table = ODSTableManifest(table_identity="db:t", source_db_id="db", source_table="t",
                             ods_table="ods_t", source_catalog_version=1)
    assert table.steady_mode == ODSSyncMode.FULL


def test_manifest_requires_cursor_for_explicit_incremental():
    try:
        ODSTableManifest(table_identity="db:t", source_db_id="db", source_table="t",
                         ods_table="ods_t", source_catalog_version=1,
                         steady_mode=ODSSyncMode.INCREMENTAL)
    except ValueError as exc:
        assert "incremental_column" in str(exc)
    else:
        raise AssertionError("incremental manifest without a cursor must be rejected")


def test_manifest_table_without_run_cannot_pass():
    gate = evaluate_ods_quality(_manifest(required=True), [])
    assert gate.status == StageStatus.FAILED
    assert gate.checks[0]["reasons"] == ["execution_missing"]
