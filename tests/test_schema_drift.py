"""Regression tests for schema-drift snapshot comparison and persistence."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from wren_agent.models.schema import (
    FieldSchema,
    SchemaExploration,
    SchemaSnapshot,
    TableSchema,
)
from wren_agent.models.task import DriftType
from wren_agent.state import create_initial_state


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_store_snapshot_parses_jsonb_text_before_comparison(monkeypatch):
    """JSONB text from asyncpg must not crash `_detect_drift().keys()`."""
    from wren_agent.tools import pg_store

    class Conn:
        executed_args = None
        executed_query = ""

        async def fetchrow(self, *_args):
            return {
                "snapshot_id": "snapshot-1",
                "fields": '{"id":"INT","removed_col":"VARCHAR"}',
            }

        async def execute(self, _query, *args):
            self.executed_query = _query
            self.executed_args = args

    conn = Conn()
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _Pool(conn))

    await pg_store.store_schema_snapshot(
        task_id="task-2",
        table_name="orders",
        source_db_id="source-1",
        fields={"id": "BIGINT", "added_col": "DATE"},
        primary_keys=["id"],
    )

    assert conn.executed_args is not None
    assert "WITH updated AS" in conn.executed_query
    assert conn.executed_args[5] is True
    drift = json.loads(conn.executed_args[6])
    assert {item["type"] for item in drift} == {
        "column_removed",
        "column_added",
        "column_type_changed",
    }


@pytest.mark.asyncio
async def test_load_latest_schema_snapshots_excludes_current_task(monkeypatch):
    from wren_agent.tools import pg_store

    class Conn:
        query = ""
        args = ()

        async def fetch(self, query, *args):
            self.query = query
            self.args = args
            return [{
                "table_name": "orders",
                "fields": '{"id":"INT"}',
                "primary_keys": ["id"],
                "snapshot_at": datetime.now(timezone.utc),
            }]

    conn = Conn()
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _Pool(conn))

    snapshots = await pg_store.load_latest_schema_snapshots("task-2", "source-1")

    assert conn.args == ("source-1", "task-2")
    assert "task_id <> $2" in conn.query
    assert snapshots["orders"].fields == {"id": "INT"}


@pytest.mark.asyncio
async def test_drift_results_preserve_previous_snapshot(monkeypatch):
    """The model field is `previous_snapshot`, not the ignored `prior_snapshot`."""
    from wren_agent.nodes.deployment import detect_schema_drifts

    previous_fields = {"id": "INT", "removed_col": "VARCHAR"}
    state = create_initial_state("task-2", "goal", [], {})
    state["prior_snapshots"] = {
        "orders": SchemaSnapshot(
            table_name="orders",
            source_db_id="source-1",
            fields=previous_fields,
        )
    }
    state["explorations"] = {
        "source-1": SchemaExploration(
            source_db_id="source-1",
            tables=[TableSchema(
                name="orders",
                db_type="mysql",
                fields=[
                    FieldSchema(name="id", data_type="BIGINT"),
                    FieldSchema(name="added_col", data_type="DATE"),
                ],
            )],
        )
    }

    stored = []

    async def load_baseline(_task_id, _source_db_id):
        return {"orders": state["prior_snapshots"]["orders"]}

    async def store_snapshot(**kwargs):
        stored.append(kwargs)

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.load_latest_schema_snapshots", load_baseline,
    )
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.store_schema_snapshot", store_snapshot,
    )

    result = await detect_schema_drifts(state)

    assert {d.drift_type for d in result["schema_drifts"]} == {
        DriftType.COLUMN_REMOVED,
        DriftType.COLUMN_ADDED,
        DriftType.COLUMN_TYPE_CHANGED,
    }
    assert all(
        drift.previous_snapshot == previous_fields
        for drift in result["schema_drifts"]
    )
    assert [call["table_name"] for call in stored] == ["orders"]
    assert "source-1:orders" in result["current_snapshots"]


@pytest.mark.asyncio
async def test_detects_table_removed_from_previous_baseline(monkeypatch):
    from wren_agent.nodes.deployment import detect_schema_drifts

    removed = SchemaSnapshot(
        table_name="legacy_orders",
        source_db_id="source-1",
        fields={"id": "INT"},
    )

    async def load_baseline(_task_id, _source_db_id):
        return {"legacy_orders": removed}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.load_latest_schema_snapshots", load_baseline,
    )

    state = create_initial_state("task-2", "goal", [], {})
    state["explorations"] = {
        "source-1": SchemaExploration(source_db_id="source-1", tables=[]),
    }
    result = await detect_schema_drifts(state)

    assert len(result["schema_drifts"]) == 1
    drift = result["schema_drifts"][0]
    assert drift.drift_type == DriftType.TABLE_REMOVED
    assert drift.is_breaking is True
    assert drift.previous_snapshot == {"id": "INT"}
    assert drift.current_snapshot == {}


@pytest.mark.asyncio
async def test_truncated_explore_does_not_flag_catalogued_table_as_removed(monkeypatch):
    """Unexplored ≠ deleted: a cap-truncated explore must not emit TABLE_REMOVED
    for tables that are still in the source catalog (task 9fcfcdbb)."""
    from wren_agent.nodes.deployment import detect_schema_drifts

    removed = SchemaSnapshot(
        table_name="financial_reporting_configuration",
        source_db_id="source-1",
        fields={"id": "INT"},
    )

    async def load_baseline(_task_id, _source_db_id):
        return {"financial_reporting_configuration": removed}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.load_latest_schema_snapshots", load_baseline,
    )

    state = create_initial_state("task-2", "goal", [], {})
    state["explorations"] = {
        "source-1": SchemaExploration(
            source_db_id="source-1",
            tables=[TableSchema(
                name="orders",
                db_type="mysql",
                fields=[FieldSchema(name="id", data_type="INT")],
            )],
            catalog_table_names=[
                "orders", "financial_reporting_configuration",
            ],
        ),
    }
    result = await detect_schema_drifts(state)

    assert not any(
        d.drift_type == DriftType.TABLE_REMOVED for d in result["schema_drifts"]
    )


@pytest.mark.asyncio
async def test_table_missing_from_catalog_is_still_removed(monkeypatch):
    from wren_agent.nodes.deployment import detect_schema_drifts

    removed = SchemaSnapshot(
        table_name="legacy_orders",
        source_db_id="source-1",
        fields={"id": "INT"},
    )

    async def load_baseline(_task_id, _source_db_id):
        return {"legacy_orders": removed}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.load_latest_schema_snapshots", load_baseline,
    )

    state = create_initial_state("task-2", "goal", [], {})
    state["explorations"] = {
        "source-1": SchemaExploration(
            source_db_id="source-1",
            tables=[],
            catalog_table_names=["orders"],
        ),
    }
    result = await detect_schema_drifts(state)

    assert len(result["schema_drifts"]) == 1
    assert result["schema_drifts"][0].drift_type == DriftType.TABLE_REMOVED


@pytest.mark.asyncio
async def test_snapshot_baseline_timeout_warns_and_continues(monkeypatch):
    from wren_agent.nodes import deployment

    async def slow_load(_task_id, _source_db_id):
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.load_latest_schema_snapshots", slow_load,
    )
    monkeypatch.setattr(deployment, "SCHEMA_SNAPSHOT_TIMEOUT_SECONDS", 0.001)

    state = create_initial_state("task-2", "goal", [], {})
    state["explorations"] = {
        "source-1": SchemaExploration(source_db_id="source-1", tables=[]),
    }
    result = await deployment.detect_schema_drifts(state)

    assert result["schema_drifts"] == []
    assert any("baseline unavailable" in w for w in result["warnings"])


def test_breaking_drift_routes_to_approval_then_back_to_design():
    from wren_agent.graph import route_after_approval, route_after_drift_fix
    from wren_agent.models.task import ApprovalItem, ApprovalStatus, SchemaDriftResult

    state = create_initial_state("task-2", "goal", [], {})
    state["schema_drifts"] = [SchemaDriftResult(
        table_name="orders",
        drift_type=DriftType.COLUMN_REMOVED,
        details="removed",
        is_breaking=True,
    )]
    state["approval_context"] = "schema_drift"
    assert route_after_drift_fix(state) == "human_approval"

    item = ApprovalItem(
        title="drift", description="breaking change",
        artifact_type="drift_fix", artifact_id="orders",
    )
    state["approval_items"] = [item]
    assert route_after_approval(state) == "human_approval"

    item.status = ApprovalStatus.APPROVED
    assert route_after_approval(state) == "design_ods"


def test_rejected_breaking_drift_stops_before_design():
    from wren_agent.graph import route_after_approval
    from wren_agent.models.task import ApprovalItem, ApprovalStatus, TaskStatus

    state = create_initial_state("task-2", "goal", [], {})
    state["approval_context"] = "schema_drift"
    item = ApprovalItem(
        title="drift", description="breaking change",
        artifact_type="drift_fix", artifact_id="orders",
    )
    item.status = ApprovalStatus.REJECTED
    state["approval_items"] = [item]

    # Route mutations are discarded by LangGraph, so the route only funnels
    # to generate_report — the report node owns the FAILED flag.
    assert route_after_approval(state) == "generate_report"
    from wren_agent.nodes.reporting import _has_rejected_breaking_drift
    assert _has_rejected_breaking_drift(state) is True


@pytest.mark.asyncio
async def test_auto_approved_drift_becomes_deployable_ods_artifact(monkeypatch):
    from wren_agent.graph import route_after_approval
    from wren_agent.models.design import DWDesignPlan
    from wren_agent.models.task import SchemaDriftResult
    from wren_agent.nodes.approval import human_approval, process_approval
    from wren_agent.nodes.design_layers import _append_approved_drift_fix_artifacts

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("wren_agent.engine.persist_approval_response", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)

    fix_sql = "ALTER TABLE ods_orders DROP COLUMN IF EXISTS legacy_col;"
    drift = SchemaDriftResult(
        table_name="orders",
        drift_type=DriftType.COLUMN_REMOVED,
        details="legacy_col removed",
        is_breaking=True,
        auto_fix_sql=fix_sql,
    )
    state = create_initial_state("task-2", "goal", [], {}, mode="auto")
    state["schema_drifts"] = [drift]
    state["approval_context"] = "schema_drift"

    state = await human_approval(state)
    assert len(state["approval_items"]) == 1
    state = await process_approval(state)
    assert drift.human_approved_fix is True
    assert route_after_approval(state) == "design_ods"

    state["design_plan"] = DWDesignPlan(task_id="task-2", modeling_goal="goal")
    _append_approved_drift_fix_artifacts(state)
    _append_approved_drift_fix_artifacts(state)

    artifacts = state["design_plan"].sql_artifacts
    assert len(artifacts) == 1
    assert artifacts[0].layer == "ODS"
    assert artifacts[0].sql == fix_sql


@pytest.mark.asyncio
async def test_human_approval_does_not_recreate_rejected_drift_fix(monkeypatch):
    from wren_agent.models.task import ApprovalItem, ApprovalStatus, SchemaDriftResult
    from wren_agent.nodes.approval import human_approval

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("wren_agent.engine.persist_approval_response", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)

    drift = SchemaDriftResult(
        table_name="orders",
        drift_type=DriftType.COLUMN_REMOVED,
        details="legacy_col removed",
        is_breaking=True,
    )
    rejected = ApprovalItem(
        title="Schema 破坏性变更确认: orders",
        description="rejected",
        artifact_type="drift_fix",
        artifact_id="orders",
        status=ApprovalStatus.REJECTED,
    )
    state = create_initial_state("task-2", "goal", [], {}, mode="auto")
    state["schema_drifts"] = [drift]
    state["approval_items"] = [rejected]

    state = await human_approval(state)

    drift_items = [
        a for a in state["approval_items"] if a.artifact_type == "drift_fix"
    ]
    assert len(drift_items) == 1
    assert drift_items[0].status == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_truncated_table_removed_cards_are_dropped_so_design_can_proceed(monkeypatch):
    """A checkpoint already sitting on false TABLE_REMOVED cards (truncated
    explore, no catalog on the exploration) must drop them on re-entry so
    recover/resume can reach design_ods instead of looping or failing."""
    from wren_agent.graph import route_after_approval
    from wren_agent.models.task import ApprovalItem, ApprovalStatus, SchemaDriftResult
    from wren_agent.nodes.approval import human_approval

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("wren_agent.engine.persist_approval_response", noop)
    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", noop)

    drift = SchemaDriftResult(
        table_name="financial_reporting_configuration",
        drift_type=DriftType.TABLE_REMOVED,
        details="Table 'financial_reporting_configuration' removed from source",
        is_breaking=True,
    )
    pending = ApprovalItem(
        title="Schema 破坏性变更确认: financial_reporting_configuration",
        description="pending twin",
        artifact_type="drift_fix",
        artifact_id="financial_reporting_configuration",
        status=ApprovalStatus.PENDING,
    )
    rejected = ApprovalItem(
        title="Schema 破坏性变更确认: financial_reporting_configuration",
        description="already rejected",
        artifact_type="drift_fix",
        artifact_id="financial_reporting_configuration",
        status=ApprovalStatus.REJECTED,
    )
    state = create_initial_state("task-2", "goal", [], {}, mode="auto")
    state["schema_drifts"] = [drift]
    state["approval_items"] = [rejected, pending]
    state["approval_context"] = "schema_drift"
    state["warnings"] = [
        "'source-1': exploring first 200 of 291 tables (AGENT_EXPLORE_MAX_TABLES)",
    ]

    state = await human_approval(state)

    assert not any(d.drift_type == DriftType.TABLE_REMOVED for d in state["schema_drifts"])
    assert not any(a.artifact_type == "drift_fix" for a in state["approval_items"])
    from wren_agent.models.task import TaskStatus
    assert state.get("status") != TaskStatus.FAILED
    assert route_after_approval(state) == "design_ods"
