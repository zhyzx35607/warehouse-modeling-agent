"""Tests for AgentState factory and structure."""

import pytest
from datetime import datetime

from wren_agent.state import create_initial_state, AgentState


class TestAgentState:
    def test_create_initial_state_defaults(self):
        state = create_initial_state(
            task_id="test-001",
            modeling_goal="Build DW",
            source_databases=[],
            dw_target={},
        )
        assert state["task_id"] == "test-001"
        assert state["modeling_goal"] == "Build DW"
        assert state["status"].value == "pending"
        assert state["phase"].value == "init"
        assert state["messages"] == []
        assert state["clarifications"] == []
        assert state["approval_items"] == []
        assert state["design_plan"] is None
        assert state["construction_report"] is None
        assert state["plain_report"] is None
        assert state["retry_count"] == 0
        assert state["max_retries"] == 3
        assert state["errors"] == []
        assert state["warnings"] == []
        assert state["warehouse_source_id"] == ""
        assert state["landing"] == {}
        assert state["create_missing_databases"] is False

    def test_create_initial_state_with_dbs(self):
        from wren_agent.models.task import SourceDatabase
        dbs = [SourceDatabase(name="test_db", db_type="mysql", host="localhost", port=3306, database="test", mcp_server_name="test_mcp")]
        state = create_initial_state("t1", "goal", dbs, {"host": "dw"})
        assert len(state["source_databases"]) == 1
        assert state["dw_target"] == {"host": "dw"}

    def test_create_initial_state_typed_dict_keys(self):
        """All expected keys should be present in initial state."""
        state = create_initial_state("t1", "goal", [], {})
        expected_keys = {
            "task_id", "thread_id", "status", "phase", "modeling_goal",
            "mode", "created_at", "updated_at", "messages", "clarifications",
            "approval_items", "pending_human_input", "human_input_type",
            "approval_context", "auto_approval_rounds",
            "source_databases", "dw_target", "explorations", "sensitive_fields",
            "deep_explore_pending", "prior_snapshots", "current_snapshots",
            "schema_drifts", "matched_blueprints", "selected_blueprint",
            "incremental_strategies", "design_plan", "validation_results",
            "all_validations_passed", "ds_project_name", "ds_data_sources",
            "ds_dags", "execution_results", "error_classifications",
            "retry_count", "max_retries", "construction_report", "plain_report",
            "kg_export_id", "data_asset_map", "errors", "warnings", "audit_log",
        }
        missing = expected_keys - set(state.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_create_initial_state_thread_id_default(self):
        state = create_initial_state("task-abc", "goal", [], {})
        assert state["thread_id"] == "task-abc"

    def test_create_initial_state_thread_id_custom(self):
        state = create_initial_state("task-1", "goal", [], {}, thread_id="custom-thread")
        assert state["thread_id"] == "custom-thread"

    def test_create_initial_state_mode_default_manual(self):
        from wren_agent.state import is_auto_mode
        state = create_initial_state("t1", "goal", [], {})
        assert state["mode"] == "manual"
        assert state["auto_approval_rounds"] == 0
        assert is_auto_mode(state) is False

    def test_create_initial_state_mode_auto(self):
        from wren_agent.state import is_auto_mode
        state = create_initial_state("t1", "goal", [], {}, mode="auto")
        assert state["mode"] == "auto"
        assert is_auto_mode(state) is True

    def test_is_auto_mode_missing_key_is_manual(self):
        """Old checkpoints without `mode` must be treated as manual."""
        from wren_agent.state import is_auto_mode
        state = create_initial_state("t1", "goal", [], {})
        del state["mode"]
        assert is_auto_mode(state) is False
