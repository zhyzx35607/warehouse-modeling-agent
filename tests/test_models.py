"""Tests for domain models — validation, serialization, defaults."""

import pytest
from datetime import datetime

from wren_agent.models.task import (
    TaskStatus, TaskPhase, ClarificationItem, ClarificationStatus,
    ApprovalItem, ApprovalStatus, ErrorClassification, ErrorType,
    ExecutionResult, IncrementalStrategy, SchemaDriftResult, DriftType,
    ValidationLevel, ValidationResult, SensorResult, SensorType,
)
from wren_agent.models.schema import (
    TableSchema, FieldSchema, TableRelationship, RelationshipType,
    SchemaExploration, SchemaSnapshot,
)
from wren_agent.models.blueprint import Blueprint, BlueprintLayer, BlueprintMatch
from wren_agent.models.design import (
    ODSDesign, DIMDesign, DWDDesign, DWSDesign, ADSDesign,
    DWDesignPlan, SQLArtifact, DataXConfig, FlinkCDCConfig,
)


class TestTaskModels:
    def test_clarification_item_defaults(self):
        c = ClarificationItem(question="test?")
        assert c.id is not None
        assert c.status == ClarificationStatus.PENDING
        assert c.blocking is True
        assert c.impact_level == "medium"
        assert c.candidates == []
        assert c.recommended is None
        assert c.recommended_reason == ""

    def test_clarification_item_recommended(self):
        c = ClarificationItem(
            question="test?", candidates=["a", "b"],
            recommended="a", recommended_reason="most common",
        )
        assert c.recommended == "a"
        assert c.recommended_reason == "most common"
        # round-trip through model_dump
        dumped = c.model_dump()
        assert dumped["recommended"] == "a"
        assert dumped["recommended_reason"] == "most common"

    def test_clarification_item_answer(self):
        c = ClarificationItem(question="test?", candidates=["a", "b"])
        c.answer = "a"
        c.status = ClarificationStatus.ANSWERED
        assert c.answer == "a"
        assert c.status == ClarificationStatus.ANSWERED

    def test_approval_item_defaults(self):
        a = ApprovalItem(title="approve", description="desc", artifact_type="design_plan")
        assert a.status == ApprovalStatus.PENDING

    def test_error_classification_transient(self):
        e = ErrorClassification(
            task_instance_id="ti-1", error_type=ErrorType.TRANSIENT,
            raw_log="connection timeout", summary="timeout", action="retry",
        )
        assert e.retry_count == 0
        assert e.max_retries == 3

    def test_execution_result_success(self):
        r = ExecutionResult(task_instance_id="ti-1", success=True)
        assert r.error is None

    def test_execution_result_failure(self):
        err = ErrorClassification(
            task_instance_id="ti-1", error_type=ErrorType.LOGIC,
            raw_log="syntax error", summary="bad SQL", action="escalate",
        )
        r = ExecutionResult(task_instance_id="ti-1", success=False, error=err)
        assert r.success is False
        assert r.error.error_type == ErrorType.LOGIC

    def test_incremental_strategy(self):
        s = IncrementalStrategy(
            table_name="orders",
            supports_incremental=True,
            incremental_column="updated_at",
        )
        assert s.table_name == "orders"
        assert s.incremental_type == "full"

    def test_schema_drift_breaking(self):
        d = SchemaDriftResult(
            table_name="orders",
            drift_type=DriftType.COLUMN_REMOVED,
            details="column 'status' removed",
            is_breaking=True,
    )
        assert d.is_breaking is True

    def test_sensor_result(self):
        s = SensorResult(table_name="users", field_name="phone", sensor_type=SensorType.PHONE)
        assert s.confidence == 0.0
        assert s.human_confirmed is False

    def test_validation_result(self):
        v = ValidationResult(
            level=ValidationLevel.STRUCTURE,
            layer="ODS",
            passed=True,
            checks=[{"check": "syntax", "detail": "ok"}],
            failures=[],
        )
        assert v.passed is True

    def test_task_status_enum(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.COMPLETED.value == "completed"

    def test_task_phase_enum_flow(self):
        phases = [p.value for p in TaskPhase]
        assert "init" in phases
        assert "design_ods" in phases
        assert "design_ads" in phases
        assert "report" in phases


class TestSchemaModels:
    def test_field_schema_defaults(self):
        f = FieldSchema(name="id", data_type="bigint", doris_type="BIGINT")
        assert f.nullable is True
        assert f.primary_key is False
        assert f.is_sensitive is False
        assert f.distinct_count is None

    def test_table_schema_pk(self):
        t = TableSchema(name="orders", db_type="mysql", fields=[
            FieldSchema(name="id", data_type="bigint", primary_key=True, doris_type="BIGINT"),
            FieldSchema(name="name", data_type="varchar", doris_type="VARCHAR"),
        ])
        assert t.primary_keys == ["id"]

    def test_table_relationship(self):
        r = TableRelationship(
            source_table="orders", source_column="user_id",
            target_table="users", target_column="id",
        )
        assert r.relationship_type == RelationshipType.UNKNOWN
        assert r.verified is False

    def test_schema_exploration(self):
        e = SchemaExploration(source_db_id="db-1")
        assert e.phase == "light"

    def test_schema_snapshot(self):
        s = SchemaSnapshot(
            table_name="orders",
            source_db_id="db-1",
            fields={"id": "bigint", "name": "varchar"},
            primary_keys=["id"],
        )
        assert s.fields == {"id": "bigint", "name": "varchar"}


class TestBlueprintModels:
    def test_blueprint(self):
        bp = Blueprint(
            name="ecommerce", description="ecommerce model",
            domain="ecommerce", tags=["电商", "交易"],
        )
        assert bp.version == 1
        assert bp.usage_count == 0

    def test_blueprint_match(self):
        m = BlueprintMatch(
            blueprint_id="bp-1", blueprint_name="ecommerce",
            score=0.85, matched_tables=["orders"],
            unmatched_tables=["products"],
        )
        assert m.score == 0.85
        assert m.selected is False


class TestDesignModels:
    def test_ods_design(self):
        o = ODSDesign(
            source_table="orders", source_db_id="db-1",
            ods_table="ods_orders",
        )
        assert o.sync_method == "datax"

    def test_dim_design(self):
        d = DIMDesign(
            dim_name="product_dim", dim_table="dim_product",
            source="from_source_table",
            natural_key=["product_id"],
        )
        assert d.registered is False
        assert d.version == 1

    def test_dwd_design(self):
        d = DWDDesign(
            fact_name="order_fact", fact_table="dwd_orders",
            grain=["order_id"],
        )
        assert d.grain == ["order_id"]

    def test_dws_design(self):
        d = DWSDesign(
            summary_name="daily_orders", summary_table="dws_orders_daily",
            source_dwd_table="dwd_orders", grain=["date"],
            refresh_logic="incremental",
        )
        assert d.refresh_logic == "incremental"

    def test_ads_design(self):
        a = ADSDesign(
            application_name="GMV Dashboard", ads_table="ads_gmv",
            metrics=[{"name": "gmv", "expression": "SUM(amount)"}],
        )
        assert len(a.metrics) == 1

    def test_dw_design_plan(self):
        plan = DWDesignPlan(task_id="task-1", modeling_goal="ecommerce")
        assert plan.approved is False
        assert plan.version == 1

    def test_sql_artifact(self):
        s = SQLArtifact(
            layer="ODS", target_table="ods_orders",
            sql_type="DDL", sql="CREATE TABLE ...",
        )
        assert s.version == 1
