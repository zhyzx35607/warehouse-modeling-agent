"""Tests for LangGraph state machine routing logic."""

import pytest

from wren_agent.state import create_initial_state
from wren_agent.models.task import (
    TaskStatus, TaskPhase, ClarificationItem, ClarificationStatus,
    ApprovalItem, ApprovalStatus, ErrorClassification, ErrorType,
    ExecutionResult, SchemaDriftResult, DriftType,
)
from wren_agent.models.schema import SchemaExploration


class TestRoutingHelpers:
    def test_has_pending_blocking_false(self):
        from wren_agent.graph import _has_pending_blocking_clarifications
        state = create_initial_state("t1", "goal", [], {})
        assert _has_pending_blocking_clarifications(state) is False

    def test_has_pending_blocking_true(self):
        from wren_agent.graph import _has_pending_blocking_clarifications
        state = create_initial_state("t1", "goal", [], {})
        state["clarifications"] = [
            ClarificationItem(question="q?", blocking=True),
            ClarificationItem(question="q2?", blocking=False),
        ]
        assert _has_pending_blocking_clarifications(state) is True

    def test_has_pending_blocking_answered(self):
        from wren_agent.graph import _has_pending_blocking_clarifications
        state = create_initial_state("t1", "goal", [], {})
        c = ClarificationItem(question="q?", blocking=True)
        c.status = ClarificationStatus.ANSWERED
        state["clarifications"] = [c]
        assert _has_pending_blocking_clarifications(state) is False

    def test_has_pending_deep_explore_false(self):
        from wren_agent.graph import _has_pending_deep_explore
        state = create_initial_state("t1", "goal", [], {})
        assert _has_pending_deep_explore(state) is False

    def test_has_pending_deep_explore_true(self):
        from wren_agent.graph import _has_pending_deep_explore
        state = create_initial_state("t1", "goal", [], {})
        state["deep_explore_pending"] = [{"type": "count_distinct"}]
        assert _has_pending_deep_explore(state) is True

    def test_has_pending_approval_false(self):
        from wren_agent.graph import _has_pending_approval
        state = create_initial_state("t1", "goal", [], {})
        assert _has_pending_approval(state) is False

    def test_has_pending_approval_true(self):
        from wren_agent.graph import _has_pending_approval
        state = create_initial_state("t1", "goal", [], {})
        state["approval_items"] = [
            ApprovalItem(title="test", description="d", artifact_type="design_plan")
        ]
        assert _has_pending_approval(state) is True

    def test_all_approvals_granted_empty(self):
        from wren_agent.graph import _all_approvals_granted
        state = create_initial_state("t1", "goal", [], {})
        assert _all_approvals_granted(state) is True

    def test_all_approvals_granted_all_approved(self):
        from wren_agent.graph import _all_approvals_granted
        state = create_initial_state("t1", "goal", [], {})
        a = ApprovalItem(title="t", description="d", artifact_type="design_plan")
        a.status = ApprovalStatus.APPROVED
        state["approval_items"] = [a]
        assert _all_approvals_granted(state) is True

    def test_all_approvals_granted_one_rejected(self):
        from wren_agent.graph import _all_approvals_granted
        state = create_initial_state("t1", "goal", [], {})
        a = ApprovalItem(title="t", description="d", artifact_type="design_plan")
        a.status = ApprovalStatus.REJECTED
        state["approval_items"] = [a]
        assert _all_approvals_granted(state) is False

    def test_has_schema_drifts_false(self):
        from wren_agent.graph import _has_schema_drifts
        state = create_initial_state("t1", "goal", [], {})
        assert _has_schema_drifts(state) is False

    def test_has_schema_drifts_true(self):
        from wren_agent.graph import _has_schema_drifts
        state = create_initial_state("t1", "goal", [], {})
        state["schema_drifts"] = [
            SchemaDriftResult(
                table_name="orders", drift_type=DriftType.COLUMN_REMOVED,
                details="col x removed", is_breaking=True,
            )
        ]
        assert _has_schema_drifts(state) is True


class TestRouteFunctions:
    def test_route_after_grill_blocking(self):
        from wren_agent.graph import route_after_grill
        state = create_initial_state("t1", "goal", [], {})
        state["clarifications"] = [ClarificationItem(question="q?", blocking=True)]
        result = route_after_grill(state)
        assert result == "wait_human"

    def test_route_after_grill_no_blocking(self):
        from wren_agent.graph import route_after_grill
        state = create_initial_state("t1", "goal", [], {})
        result = route_after_grill(state)
        assert result == "detect_schema_drifts"

    def test_route_after_validation_passed(self):
        from wren_agent.models.design import DWDesignPlan
        from wren_agent.graph import route_after_validation
        state = create_initial_state("t1", "goal", [], {})
        state["design_plan"] = DWDesignPlan(task_id="t1", modeling_goal="goal")
        state["all_validations_passed"] = True
        result = route_after_validation(state)
        assert result == "human_approval"

    def test_route_after_validation_redeploys_when_plan_already_approved(self):
        """After design_revise, skip re-approval and redeploy (task b131f22e)."""
        from wren_agent.models.design import DWDesignPlan
        from wren_agent.graph import route_after_validation
        state = create_initial_state("t1", "goal", [], {})
        plan = DWDesignPlan(task_id="t1", modeling_goal="goal")
        plan.approved = True
        state["design_plan"] = plan
        state["all_validations_passed"] = True
        assert route_after_validation(state) == "dag_deploy"

    def test_route_after_validation_failed(self):
        from wren_agent.models.design import DWDesignPlan, SQLArtifact
        from wren_agent.graph import route_after_validation
        state = create_initial_state("t1", "goal", [], {})
        state["design_plan"] = DWDesignPlan(
            task_id="t1",
            modeling_goal="goal",
            sql_artifacts=[SQLArtifact(
                layer="ODS", target_table="t", sql_type="DDL", sql="SELECT 1"
            )],
        )
        state["all_validations_passed"] = False
        result = route_after_validation(state)
        assert result == "design_revise"

    def test_route_after_validation_revision_limit_escalates(self):
        """Regression: design_revise ↔ harness_validate must not loop until
        GRAPH_RECURSION_LIMIT kills the task — escalate to human instead."""
        from wren_agent.models.design import DWDesignPlan, SQLArtifact
        from wren_agent.graph import MAX_DESIGN_REVISIONS, route_after_validation
        state = create_initial_state("t1", "goal", [], {})
        state["design_plan"] = DWDesignPlan(
            task_id="t1",
            modeling_goal="goal",
            sql_artifacts=[SQLArtifact(
                layer="ODS", target_table="t", sql_type="DDL", sql="SELECT 1"
            )],
        )
        state["all_validations_passed"] = False
        state["design_revision_count"] = MAX_DESIGN_REVISIONS
        assert route_after_validation(state) == "human_approval"

    def test_route_after_validation_under_revision_limit_revises(self):
        from wren_agent.models.design import DWDesignPlan, SQLArtifact
        from wren_agent.graph import MAX_DESIGN_REVISIONS, route_after_validation
        state = create_initial_state("t1", "goal", [], {})
        state["design_plan"] = DWDesignPlan(
            task_id="t1",
            modeling_goal="goal",
            sql_artifacts=[SQLArtifact(
                layer="ODS", target_table="t", sql_type="DDL", sql="SELECT 1"
            )],
        )
        state["all_validations_passed"] = False
        state["design_revision_count"] = MAX_DESIGN_REVISIONS - 1
        assert route_after_validation(state) == "design_revise"

    @pytest.mark.asyncio
    async def test_design_revise_increments_revision_count(self):
        from wren_agent.nodes.validation import design_revise
        state = create_initial_state("t1", "goal", [], {})
        state = await design_revise(state)
        assert state["design_revision_count"] == 1
        state = await design_revise(state)
        assert state["design_revision_count"] == 2

    def test_validation_result_allows_none_passed(self):
        """Regression: semantic validation uses passed=None (needs human
        review) — used to raise pydantic bool_type and crash the node."""
        from wren_agent.models.task import ValidationLevel, ValidationResult
        r = ValidationResult(level=ValidationLevel.SEMANTIC, layer="ADS", passed=None)
        assert r.passed is None

    def test_route_after_execution_all_success(self):
        from wren_agent.graph import route_after_execution
        state = create_initial_state("t1", "goal", [], {})
        state["execution_results"] = [
            ExecutionResult(task_instance_id="ti-1", success=True),
            ExecutionResult(task_instance_id="ti-2", success=True),
        ]
        result = route_after_execution(state)
        assert result == "final_validate"

    def test_route_after_execution_partial_fail(self):
        from wren_agent.graph import route_after_execution
        state = create_initial_state("t1", "goal", [], {})
        state["execution_results"] = [
            ExecutionResult(task_instance_id="ti-1", success=True),
            ExecutionResult(task_instance_id="ti-2", success=False),
        ]
        result = route_after_execution(state)
        assert result == "classify_errors"

    def test_route_after_approval_granted(self):
        from wren_agent.graph import route_after_approval
        state = create_initial_state("t1", "goal", [], {})
        a = ApprovalItem(title="t", description="d", artifact_type="design_plan")
        a.status = ApprovalStatus.APPROVED
        state["approval_items"] = [a]
        result = route_after_approval(state)
        assert result == "dag_deploy"

    def test_route_after_approval_rejected(self):
        from wren_agent.graph import route_after_approval
        state = create_initial_state("t1", "goal", [], {})
        a = ApprovalItem(title="t", description="d", artifact_type="design_plan")
        a.status = ApprovalStatus.REJECTED
        state["approval_items"] = [a]
        result = route_after_approval(state)
        assert result == "design_revise"

    def test_route_after_final_validate_passed(self):
        from wren_agent.graph import route_after_final_validate
        state = create_initial_state("t1", "goal", [], {})
        state["all_validations_passed"] = True
        result = route_after_final_validate(state)
        assert result == "quality_monitor"

    def test_route_after_final_validate_failed(self):
        from wren_agent.graph import route_after_final_validate
        state = create_initial_state("t1", "goal", [], {})
        state["all_validations_passed"] = False
        result = route_after_final_validate(state)
        assert result == "error_handling"

    def test_route_after_error_handling_failed_reports(self):
        """Failed tasks get a failure report instead of ending silently."""
        from wren_agent.graph import route_after_error_handling
        state = create_initial_state("t1", "goal", [], {})
        state["status"] = TaskStatus.FAILED
        assert route_after_error_handling(state) == "generate_report"

    def test_route_after_error_handling_recoverable_requests_approval(self):
        from wren_agent.graph import route_after_error_handling
        state = create_initial_state("t1", "goal", [], {})
        assert route_after_error_handling(state) == "human_approval"

    def test_failfast_routes_funnel_failed_tasks_to_report(self):
        """Every fail-fast route sends FAILED tasks to generate_report so a
        failure report is always produced (no more silent dead ends)."""
        from wren_agent.graph import (
            route_after_schema_explore, route_after_design,
            route_after_validation, route_after_approval,
            route_after_deploy, route_after_error_handling,
        )
        for route in (route_after_schema_explore, route_after_design,
                      route_after_validation, route_after_approval,
                      route_after_deploy, route_after_error_handling):
            state = create_initial_state("t1", "goal", [], {})
            state["status"] = TaskStatus.FAILED
            assert route(state) == "generate_report", route.__name__

    def test_route_after_design_cancelled_ends(self):
        from wren_agent.graph import route_after_design
        state = create_initial_state("t1", "goal", [], {})
        state["status"] = TaskStatus.CANCELLED
        assert route_after_design(state) == "__end__"

    def test_route_after_report_failed_ends(self):
        from wren_agent.graph import route_after_report
        state = create_initial_state("t1", "goal", [], {})
        state["status"] = TaskStatus.FAILED
        assert route_after_report(state) == "__end__"

    def test_route_after_report_success_exports_kg(self):
        from wren_agent.graph import route_after_report
        state = create_initial_state("t1", "goal", [], {})
        state["status"] = TaskStatus.COMPLETED
        assert route_after_report(state) == "export_kg"

    def test_route_after_approval_rejected_drift_reports(self):
        """Rejected breaking drift goes to generate_report; the node owns the
        FAILED flag (route mutations are discarded by LangGraph)."""
        from wren_agent.graph import route_after_approval
        state = create_initial_state("t1", "goal", [], {})
        state["approval_context"] = "schema_drift"
        a = ApprovalItem(title="t", description="d", artifact_type="drift_fix")
        a.status = ApprovalStatus.REJECTED
        state["approval_items"] = [a]
        assert route_after_approval(state) == "generate_report"

    def test_route_after_quality_monitor_passed_completes(self):
        from wren_agent.graph import route_after_quality_monitor
        state = create_initial_state("t1", "goal", [], {})
        state["quality_checks"] = {"failed": 0, "errored": 0}
        assert route_after_quality_monitor(state) == "generate_report"
        assert state["status"] == TaskStatus.COMPLETED

    def test_route_after_quality_monitor_failure_reports_as_failed(self):
        from wren_agent.graph import route_after_quality_monitor
        state = create_initial_state("t1", "goal", [], {})
        state["quality_checks"] = {"failed": 1, "errored": 0}
        assert route_after_quality_monitor(state) == "generate_report"
        assert state["status"] == TaskStatus.FAILED

    def test_route_after_human_response_no_blocking(self):
        from wren_agent.graph import route_after_human_response
        state = create_initial_state("t1", "goal", [], {})
        result = route_after_human_response(state)
        assert result == "detect_schema_drifts"

    def test_route_after_human_response_still_blocking(self):
        from wren_agent.graph import route_after_human_response
        state = create_initial_state("t1", "goal", [], {})
        state["clarifications"] = [ClarificationItem(question="q?", blocking=True)]
        result = route_after_human_response(state)
        assert result == "wait_human"

    def test_route_after_deep_explore_pending(self):
        from wren_agent.graph import route_after_deep_explore
        state = create_initial_state("t1", "goal", [], {})
        state["deep_explore_pending"] = [{"type": "test"}]
        result = route_after_deep_explore(state)
        assert result == "deep_explore"

    def test_route_after_deep_explore_none(self):
        from wren_agent.graph import route_after_deep_explore
        state = create_initial_state("t1", "goal", [], {})
        result = route_after_deep_explore(state)
        assert result == "grill_clarifications"

    def test_route_after_sensitive_fields_detected(self):
        from wren_agent.graph import route_after_sensitive_fields
        from wren_agent.models.task import SensorResult, SensorType
        state = create_initial_state("t1", "goal", [], {})
        state["sensitive_fields"] = [
            SensorResult(table_name="users", field_name="phone", sensor_type=SensorType.PHONE)
        ]
        result = route_after_sensitive_fields(state)
        assert result == "confirm_sensitive"

    def test_route_after_sensitive_fields_none(self):
        from wren_agent.graph import route_after_sensitive_fields
        state = create_initial_state("t1", "goal", [], {})
        result = route_after_sensitive_fields(state)
        assert result == "human_confirm_deep"

    def test_route_after_error_classify_transient_under_limit(self):
        from wren_agent.graph import route_after_error_classify
        state = create_initial_state("t1", "goal", [], {})
        state["error_classifications"] = [
            ErrorClassification(task_instance_id="ti-1", error_type=ErrorType.TRANSIENT,
                                raw_log="timeout", summary="timeout", action="retry")
        ]
        state["retry_count"] = 0
        result = route_after_error_classify(state)
        assert result == "retry_execution"

    def test_route_after_error_classify_transient_exceeded(self):
        from wren_agent.graph import route_after_error_classify
        state = create_initial_state("t1", "goal", [], {})
        state["error_classifications"] = [
            ErrorClassification(task_instance_id="ti-1", error_type=ErrorType.TRANSIENT,
                                raw_log="timeout", summary="timeout", action="retry")
        ]
        state["retry_count"] = 3
        result = route_after_error_classify(state)
        assert result == "human_approval"

    def test_route_after_error_classify_resource(self):
        from wren_agent.graph import route_after_error_classify
        state = create_initial_state("t1", "goal", [], {})
        state["error_classifications"] = [
            ErrorClassification(task_instance_id="ti-1", error_type=ErrorType.RESOURCE,
                                raw_log="disk full", summary="disk", action="wait")
        ]
        result = route_after_error_classify(state)
        assert result == "wait_human"

    def test_route_after_error_classify_fatal(self):
        from wren_agent.graph import route_after_error_classify
        state = create_initial_state("t1", "goal", [], {})
        state["error_classifications"] = [
            ErrorClassification(task_instance_id="ti-1", error_type=ErrorType.LOGIC,
                                raw_log="syntax error", summary="bad sql", action="escalate")
        ]
        # Logic errors are agent-repairable: route to design_revise while
        # revision budget remains (blind human approval re-ran the same broken
        # SQL in a loop — task 57d84a75).
        result = route_after_error_classify(state)
        assert result == "design_revise"
        # Budget exhausted → fail report (do not open approval→redeploy loop)
        state["design_revision_count"] = 99
        assert route_after_error_classify(state) == "generate_report"
        # Config errors are not SQL-repairable → straight to human
        state["design_revision_count"] = 0
        state["error_classifications"] = [
            ErrorClassification(task_instance_id="ti-2", error_type=ErrorType.CONFIG,
                                raw_log="worker down", summary="env", action="escalate")
        ]
        assert route_after_error_classify(state) == "human_approval"


class TestGraphCompile:
    def test_graph_compiles_with_30_nodes(self):
        from wren_agent.graph import build_modeling_agent_graph
        graph = build_modeling_agent_graph()
        nodes = list(graph.nodes.keys())
        # 29 functional nodes + __start__
        assert len(nodes) == 30

    def test_graph_has_all_design_nodes(self):
        from wren_agent.graph import build_modeling_agent_graph
        graph = build_modeling_agent_graph()
        nodes = set(graph.nodes.keys())
        for layer in ["ods", "dim", "dwd", "dws", "ads"]:
            assert f"design_{layer}" in nodes

    def test_graph_has_all_hitl_nodes(self):
        from wren_agent.graph import build_modeling_agent_graph
        graph = build_modeling_agent_graph()
        nodes = set(graph.nodes.keys())
        hitl_nodes = ["human_confirm_deep", "wait_human", "human_approval"]
        for n in hitl_nodes:
            assert n in nodes

    def test_graph_entry_point(self):
        from wren_agent.graph import build_modeling_agent_graph
        graph = build_modeling_agent_graph()
        # Entry should be receive_task
        # (Can't easily check edges without inspecting graph internals,
        #  but compilation succeeds which validates the structure)
        assert graph is not None

    def test_every_registered_node_is_reachable_from_entry(self):
        """Registered-but-unreachable nodes silently disappear at runtime."""
        from collections import defaultdict, deque
        from wren_agent.graph import build_modeling_agent_graph

        graph = build_modeling_agent_graph().get_graph()
        adjacency = defaultdict(set)
        for edge in graph.edges:
            adjacency[edge.source].add(edge.target)

        reachable = {"__start__"}
        queue = deque(reachable)
        while queue:
            for target in adjacency[queue.popleft()]:
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)

        assert set(graph.nodes) - reachable == set()

    def test_graph_routing_functions_coverage(self):
        """All routing functions should return valid node names."""
        from wren_agent.graph import (
            route_after_schema_drift, route_after_drift_fix,
            route_after_deep_explore, route_after_grill,
            route_after_human_response, route_after_validation,
            route_after_execution, route_after_error_classify,
            route_after_approval, route_after_final_validate,
            route_after_sensitive_fields,
        )
        state = create_initial_state("t1", "goal", [], {})
        valid_nodes = {
            "generate_drift_fixes", "design_ods", "human_approval",
            "deep_explore", "grill_clarifications", "wait_human",
            "design_revise", "dag_deploy", "final_validate",
            "classify_errors", "retry_execution", "error_handling",
            "generate_report", "process_approval", "confirm_sensitive",
            "human_confirm_deep", "detect_schema_drifts", "__end__",
        }
        # All routing functions should return valid nodes for the empty state
        all_funcs = [
            route_after_sensitive_fields(state),
            route_after_deep_explore(state),
            route_after_grill(state),
            route_after_human_response(state),
            route_after_validation(state),
        ]
        for result in all_funcs:
            assert result in valid_nodes, f"Invalid node: {result}"

    def test_branch_maps_cover_all_route_returns(self):
        """Every value a route function can return at runtime must be present
        in the compiled branch map (LangGraph infers it from the Literal
        annotation — a mismatch crashes the graph at runtime).

        Regression for: route_after_validation returning "design_revise" and
        route_after_error_classify returning "final_validate" while missing
        from their Literal annotations → task failed mid-run.
        """
        from wren_agent.graph import build_modeling_agent_graph
        g = build_modeling_agent_graph()

        # Runtime-reachable return values per conditional-edge source node
        expected = {
            "harness_validate": {"human_approval", "design_revise",
                                 "generate_report", "dag_deploy"},
            "classify_errors": {"retry_execution", "human_approval",
                                "wait_human", "dag_deploy", "final_validate",
                                "design_revise", "generate_report"},
            "grill_clarifications": {"wait_human", "detect_schema_drifts"},
            "wait_human": {"wait_human", "detect_schema_drifts"},
            "process_approval": {"human_approval", "design_ods",
                                 "dag_deploy", "design_revise",
                                 "generate_report"},
            "execute_tasks": {"classify_errors", "final_validate"},
            "final_validate": {"quality_monitor", "error_handling"},
            "design_ads": {"wait_human", "harness_validate",
                           "generate_report", "__end__"},
            "error_handling": {"human_approval", "generate_report"},
            "quality_monitor": {"generate_report"},
            "generate_report": {"export_kg", "__end__"},
            "schema_explore_light": {"detect_sensitive_fields",
                                     "generate_report"},
            "dag_deploy": {"execute_tasks", "error_handling",
                           "generate_report"},
        }
        for source, returns in expected.items():
            branches = g.builder.branches.get(source, {})
            assert branches, f"No conditional branch registered for {source}"
            for branch in branches.values():
                ends = set((branch.ends or {}).values())
                missing = returns - ends
                assert not missing, (
                    f"{source}: route can return {missing} but branch map "
                    f"only allows {ends} — runtime crash"
                )
