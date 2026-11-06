"""Tests for the task-6407879b failure-chain fixes.

Covers the root-cause chain: datasource id dropped at task creation →
credential lookup miss → empty exploration silently cascading into empty
layer designs → zero-column ADS DDL → unbounded deploy loop in auto mode.
"""

import asyncio

import pytest

from wren_agent.models.design import DWDesignPlan
from wren_agent.models.schema import SchemaExploration
from wren_agent.models.task import SourceDatabase, TaskStatus
from wren_agent.state import create_initial_state


@pytest.fixture
def offline(monkeypatch):
    """Isolate nodes from PG/WS — patch engine persistence helpers."""
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("wren_agent.engine.update_task_pg_status", _noop)
    monkeypatch.setattr("wren_agent.engine.persist_approval_response", _noop)


def _source_db(**overrides) -> dict:
    base = dict(
        name="ecommerce_mysql",
        db_type="mysql",
        host="127.0.0.1",
        port=31205,
        database="ecommerce",
        mcp_server_name="mcp_ecommerce",
    )
    base.update(overrides)
    return base


# ── A: create_task keeps the registry datasource id ─────────────


class TestBuildSourceDatabases:
    def test_registry_id_is_preserved(self):
        from wren_agent.api.routes import _build_source_databases

        dbs = _build_source_databases([_source_db(id="790cc944-6044-40c2-8735-06ee025e083f")])
        assert dbs[0].id == "790cc944-6044-40c2-8735-06ee025e083f"

    def test_missing_id_gets_generated_uuid(self):
        from wren_agent.api.routes import _build_source_databases

        dbs = _build_source_databases([_source_db()])
        assert dbs[0].id  # default_factory still applies for ad-hoc sources
        assert dbs[0].name == "ecommerce_mysql"


# ── B: credential lookup falls back to name and never stays silent ──


class TestExtractDbParams:
    @pytest.mark.asyncio
    async def test_lookup_by_id_then_name(self, monkeypatch):
        from wren_agent.nodes import explore

        calls = []

        async def fake_lookup(key):
            calls.append(key)
            if key == "ecommerce_mysql":
                return {"user": "root", "password": "pw", "db_type": "mysql",
                        "host": "h", "port": 1, "database": "d", "id": "x", "name": key}
            return None

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", fake_lookup
        )

        sdb = SourceDatabase(**_source_db())  # random uuid id, registered name
        params = await explore._extract_db_params(sdb)

        assert calls == [sdb.id, "ecommerce_mysql"]  # id first, then name fallback
        assert params["user"] == "root"
        assert params["credential_source"] == "registry"

    @pytest.mark.asyncio
    async def test_unregistered_source_marked_missing(self, monkeypatch):
        from wren_agent.nodes import explore

        async def fake_lookup(key):
            return None

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", fake_lookup
        )

        params = await explore._extract_db_params(SourceDatabase(**_source_db()))
        assert params["credential_source"] == "missing"
        assert params["user"] == ""

    @pytest.mark.asyncio
    async def test_lookup_error_marked(self, monkeypatch):
        from wren_agent.nodes import explore

        async def boom(key):
            raise RuntimeError("PG down")

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", boom
        )

        params = await explore._extract_db_params(SourceDatabase(**_source_db()))
        assert params["credential_source"] == "lookup_error"

    @pytest.mark.asyncio
    async def test_missing_credentials_never_connect(self, monkeypatch, offline):
        """schema_explore_light must not open a connection with empty creds."""
        from wren_agent.nodes import explore

        async def fake_lookup(key):
            return None

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", fake_lookup
        )

        connected = []

        class FakeConnector:
            def __init__(self, **kwargs):
                connected.append(kwargs)

        monkeypatch.setattr("wren_agent.tools.db_explorer.DBConnector", FakeConnector)

        state = create_initial_state(
            "t-missing-creds", "goal", [SourceDatabase(**_source_db())], {},
        )
        result = await explore.schema_explore_light(state)

        assert connected == []
        assert any("数据源未注册或凭据缺失" in e for e in result["errors"])


# ── C: exploration fail-fast gate ────────────────────────────────


class TestExploreFailFast:
    @pytest.mark.asyncio
    async def test_zero_tables_fails_task(self, monkeypatch, offline):
        from wren_agent.graph import route_after_schema_explore
        from wren_agent.nodes import explore

        async def fake_lookup(key):
            return None

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", fake_lookup
        )

        state = create_initial_state(
            "t-failfast", "goal", [SourceDatabase(**_source_db())], {},
        )
        result = await explore.schema_explore_light(state)

        assert result["status"] == TaskStatus.FAILED
        assert route_after_schema_explore(result) == "generate_report"

    @pytest.mark.asyncio
    async def test_no_source_dbs_does_not_trip_gate(self, offline):
        """Zero configured sources is receive_task's problem, not the gate's."""
        from wren_agent.graph import route_after_schema_explore
        from wren_agent.nodes import explore

        state = create_initial_state("t-nosrc", "goal", [], {})
        result = await explore.schema_explore_light(state)

        assert result.get("status") != TaskStatus.FAILED
        assert route_after_schema_explore(result) == "detect_sensitive_fields"


# ── D: no zero-column ADS placeholder, empty artifacts fail validation ──


class TestEmptyDesignGuards:
    @pytest.mark.asyncio
    async def test_business_design_ods_initializes_plan(self, monkeypatch, offline):
        """A warehouse-only local import must not shadow DWDesignPlan."""
        from wren_agent.nodes import design_layers

        async def empty_llm_result(*_args, **_kwargs):
            return {"designs": [], "warnings": []}

        async def noop(*_args, **_kwargs):
            return None

        monkeypatch.setattr(design_layers, "_llm_generate_layer", empty_llm_result)
        monkeypatch.setattr(design_layers, "_sandbox_validate", noop)
        monkeypatch.setattr(design_layers, "_llm_review_layer", noop)

        state = create_initial_state("t-ods-init", "goal", [], {})
        result = await design_layers.design_ods(state)

        assert isinstance(result["design_plan"], DWDesignPlan)

    @pytest.mark.asyncio
    async def test_design_ads_skips_when_no_metrics(self, offline, monkeypatch):
        from wren_agent.nodes.design_layers import design_ads

        async def fake_plan(state, layer):
            return ["ads_x"], {}

        async def fake_single(state, layer):
            return {"skip": True, "warnings": ["no metrics available"]}

        monkeypatch.setattr(
            "wren_agent.nodes.design_layers._plan_layer_tables", fake_plan)
        monkeypatch.setattr(
            "wren_agent.nodes.design_layers._llm_generate_layer", fake_single)

        state = create_initial_state("t-ads", "goal", [], {})
        state["design_plan"] = DWDesignPlan(task_id="t-ads", modeling_goal="goal")

        result = await design_ads(state)

        plan = result["design_plan"]
        assert plan.ads_designs == []
        assert not (plan.sql_artifacts or [])  # no zero-column DDL emitted
        assert any("skipping ADS table generation" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_harness_validate_fails_on_empty_artifacts(self, offline):
        from wren_agent.nodes.validation import harness_validate

        state = create_initial_state("t-val", "goal", [], {})
        state["design_plan"] = DWDesignPlan(task_id="t-val", modeling_goal="goal")

        result = await harness_validate(state)

        assert result["all_validations_passed"] is False
        assert any("no SQL artifacts" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_missing_design_plan_fails_before_empty_approval(self, offline):
        from wren_agent.graph import route_after_validation
        from wren_agent.nodes.validation import harness_validate

        state = create_initial_state("t-no-plan", "goal", [], {})
        state["design_revision_count"] = 3

        # The node owns fail-fast marking; the route funnels FAILED tasks
        # to the failure report instead of ending silently.
        result = await harness_validate(state)
        assert route_after_validation(result) == "generate_report"
        assert result["status"] == TaskStatus.FAILED
        assert any("Missing design plan" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_human_approval_without_items_fails_instead_of_pausing(
        self, monkeypatch, offline
    ):
        from wren_agent.nodes.approval import human_approval

        async def must_not_pause(*_args, **_kwargs):
            pytest.fail("empty approval entered a human wait")

        monkeypatch.setattr("wren_agent.tools.pg_store.pause_task", must_not_pause)

        state = create_initial_state("t-empty-approval", "goal", [], {})
        result = await human_approval(state)

        assert result["status"] == TaskStatus.FAILED
        assert result["pending_human_input"] is False
        assert result["approval_items"] == []


# ── E: empty design summary skips the LLM review ─────────────────


class TestLLMReviewSkip:
    def test_empty_summary_is_empty_string(self):
        from wren_agent.nodes.design_layers import _build_design_summary

        plan = DWDesignPlan(task_id="t", modeling_goal="goal")
        for layer in ("ODS", "DIM", "DWD", "DWS", "ADS"):
            assert _build_design_summary(plan, layer) == ""

    @pytest.mark.asyncio
    async def test_review_skips_llm_when_nothing_designed(self, monkeypatch):
        from wren_agent.nodes.design_layers import _llm_review_layer

        called = []

        def fake_get_client():
            called.append(True)
            raise AssertionError("LLM client must not be created for empty designs")

        monkeypatch.setattr(
            "wren_agent.tools.llm_client.get_llm_client", fake_get_client
        )

        state = create_initial_state("t-skip", "goal", [], {})
        state["design_plan"] = DWDesignPlan(task_id="t-skip", modeling_goal="goal")

        await _llm_review_layer(state, "ODS")
        assert called == []


# ── F: deploy circuit breaker ────────────────────────────────────


class TestDeployCircuitBreaker:
    @pytest.mark.asyncio
    async def test_auto_mode_fails_after_max_failures(self, offline):
        from wren_agent.graph import route_after_deploy
        from wren_agent.nodes.deployment import MAX_DEPLOY_FAILURES, _mark_deploy_failure

        state = create_initial_state("t-deploy", "goal", [], {}, mode="auto")

        await _mark_deploy_failure(state, "boom #1")
        assert state.get("status") != TaskStatus.FAILED
        assert route_after_deploy(state) == "error_handling"

        await _mark_deploy_failure(state, "boom #2")
        assert state["deploy_failure_count"] == MAX_DEPLOY_FAILURES
        assert state["status"] == TaskStatus.FAILED
        assert route_after_deploy(state) == "generate_report"

    @pytest.mark.asyncio
    async def test_manual_mode_also_trips_breaker(self, offline):
        """Regression (task 40138835): manual-mode deploy failures used to
        loop error_handling → human_approval → re-deploy forever, re-surfacing
        the approval each round. The breaker now fires for both modes."""
        from wren_agent.graph import route_after_deploy
        from wren_agent.nodes.deployment import MAX_DEPLOY_FAILURES, _mark_deploy_failure

        state = create_initial_state("t-deploy-m", "goal", [], {}, mode="manual")

        await _mark_deploy_failure(state, "boom #1")
        assert state.get("status") != TaskStatus.FAILED
        assert route_after_deploy(state) == "error_handling"

        await _mark_deploy_failure(state, "boom #2")
        assert state["deploy_failure_count"] == MAX_DEPLOY_FAILURES
        assert state["status"] == TaskStatus.FAILED
        assert route_after_deploy(state) == "generate_report"

    def test_route_after_deploy_success_path(self):
        from wren_agent.graph import route_after_deploy

        state = create_initial_state("t-deploy-ok", "goal", [], {})
        state["deploy_succeeded"] = True
        assert route_after_deploy(state) == "execute_tasks"

    def test_route_after_execution_empty_results_is_failure(self):
        """all([]) is vacuously True — empty results must not look like success."""
        from wren_agent.graph import route_after_execution

        state = create_initial_state("t-exec", "goal", [], {})
        state["execution_results"] = []
        assert route_after_execution(state) == "classify_errors"

    def test_synthetic_failure_result(self):
        from wren_agent.nodes.deployment import _synthetic_failure

        r = _synthetic_failure("no_project_code", "deployment did not complete")
        assert r.success is False
        assert r.error is not None
        assert r.error.action == "escalate"


# ── G: DSApi create_or_get_project falls back to lookup ──────────


class TestDSApiProjectIdempotent:
    @pytest.mark.asyncio
    async def test_create_falls_back_to_lookup_when_exists(self, monkeypatch):
        """Re-deploys reuse wren_{task_id[:8]} — 'already exists' must resolve
        the existing project instead of failing the deploy."""
        from cli_anything.dolphinscheduler.core.errors import APIError
        from wren_agent.tools.ds_api import DSApi

        class FakeClient:
            def close(self): ...

        api = DSApi(client=FakeClient())

        def fake_create(client, name, description):
            raise APIError("project already exists", api_code=10018)

        def fake_get_by_name(client, name):
            return {"code": 12345, "name": name}

        monkeypatch.setattr(
            "cli_anything.dolphinscheduler.core.projects.create_project", fake_create
        )
        monkeypatch.setattr(
            "cli_anything.dolphinscheduler.core.projects.get_project_by_name", fake_get_by_name
        )

        project = await api.create_or_get_project("wren_19bf8214", "desc")
        assert project["code"] == 12345

    @pytest.mark.asyncio
    async def test_network_error_propagates(self, monkeypatch):
        """NetworkError (DS unreachable) must NOT be swallowed by the
        already-exists fallback — dag_deploy needs to see the real failure."""
        from cli_anything.dolphinscheduler.core.errors import NetworkError
        from wren_agent.tools.ds_api import DSApi

        class FakeClient:
            def close(self): ...

        api = DSApi(client=FakeClient())

        def fake_create(client, name, description):
            raise NetworkError("Could not reach DolphinScheduler")

        monkeypatch.setattr(
            "cli_anything.dolphinscheduler.core.projects.create_project", fake_create
        )

        with pytest.raises(NetworkError):
            await api.create_or_get_project("wren_19bf8214", "desc")


# ── H: ds_project_code declared in AgentState (Fix 1) ───────────


class TestDsProjectCodeInState:
    def test_field_declared_in_typeddict(self):
        """ds_project_code must be a declared key so LangGraph preserves it."""
        from wren_agent.state import AgentState
        assert "ds_project_code" in AgentState.__annotations__

    def test_initial_state_has_none(self):
        state = create_initial_state("t-pc", "goal", [], {})
        assert state["ds_project_code"] is None

    def test_field_survives_round_trip(self):
        """Simulate dag_deploy writing the code and execute_tasks reading it."""
        state = create_initial_state("t-pc2", "goal", [], {})
        state["ds_project_code"] = 179151799462688
        assert state.get("ds_project_code") == 179151799462688


# ── I: create_schedule_online idempotent (Fix 2) ────────────────


class TestScheduleIdempotent:
    @pytest.mark.asyncio
    async def test_already_exist_resolves_existing_schedule(self, monkeypatch):
        from cli_anything.dolphinscheduler.core.errors import APIError
        from wren_agent.tools.ds_api import DSApi

        class FakeClient:
            def close(self): ...

        api = DSApi(client=FakeClient())

        def fake_create_schedule(client, project_code, workflow_code, crontab, **kw):
            raise APIError("schedule 8 already exist, please update or delete it", api_code=10182)

        def fake_list_schedules(client, project_code, *, workflow_definition_code=None, **kw):
            return {"totalList": [{"id": 8, "workflowDefinitionCode": workflow_definition_code}]}

        set_state_calls = []

        def fake_set_state(client, project_code, schedule_id, online):
            set_state_calls.append((schedule_id, online))

        monkeypatch.setattr("cli_anything.dolphinscheduler.core.schedules.create_schedule", fake_create_schedule)
        monkeypatch.setattr("cli_anything.dolphinscheduler.core.schedules.list_schedules", fake_list_schedules)
        monkeypatch.setattr("cli_anything.dolphinscheduler.core.schedules.set_schedule_state", fake_set_state)

        sched_id = await api.create_schedule_online(12345, 179151799462688, "0 0 0 * * ? *")
        assert sched_id == 8
        assert set_state_calls == [(8, True)]

    @pytest.mark.asyncio
    async def test_non_exist_error_propagates(self, monkeypatch):
        from cli_anything.dolphinscheduler.core.errors import APIError
        from wren_agent.tools.ds_api import DSApi

        class FakeClient:
            def close(self): ...

        api = DSApi(client=FakeClient())

        def fake_create_schedule(client, project_code, workflow_code, crontab, **kw):
            raise APIError("project not found", api_code=10001)

        monkeypatch.setattr("cli_anything.dolphinscheduler.core.schedules.create_schedule", fake_create_schedule)

        with pytest.raises(APIError, match="project not found"):
            await api.create_schedule_online(12345, 99, "0 0 0 * * ? *")


# ── J: classify_errors reuses synthetic failure classification (Fix 3) ──


class TestClassifyErrorsPreservesExisting:
    @pytest.mark.asyncio
    async def test_synthetic_failure_not_reclassified(self, monkeypatch):
        """A synthetic ExecutionResult with error already set must not be
        passed through classify_execution_error (which would mis-classify
        empty logs as LOGIC and re-route to dag_deploy)."""
        from wren_agent.models.task import ErrorClassification, ErrorType, ExecutionResult
        from wren_agent.nodes.deployment import classify_errors

        classify_calls = []

        def fake_classify(task_instance_id, raw_log, retry_count=0):
            classify_calls.append(task_instance_id)
            from wren_agent.models.task import ErrorClassification, ErrorType
            return ErrorClassification(
                task_instance_id=task_instance_id,
                error_type=ErrorType.LOGIC,
                raw_log=raw_log,
                summary="SQL logic error (possibly agent-generated)",
                action="escalate",
            )

        monkeypatch.setattr(
            "wren_agent.tools.dolphin_scheduler.classify_execution_error", fake_classify
        )

        state = create_initial_state("t-cls", "goal", [], {})
        state["execution_results"] = [
            ExecutionResult(
                task_instance_id="no_project_code",
                success=False,
                error=ErrorClassification(
                    task_instance_id="no_project_code",
                    error_type=ErrorType.CONFIG,
                    raw_log="No project_code in state",
                    summary="No project_code in state",
                    action="escalate",
                ),
            )
        ]

        result = await classify_errors(state)

        # classify_execution_error must NOT have been called for the synthetic failure
        assert classify_calls == []
        assert len(result["error_classifications"]) == 1
        assert result["error_classifications"][0].error_type == ErrorType.CONFIG

def test_classify_platform_error_is_config_not_sql():
    """Regression def559ca: a Dolphin Scheduler platform error (dependent-task
    context write failing with a null id in t_ds_task_instance_context) must
    classify as CONFIG (ops issue) — NOT as DATA/LOGIC, which would send it
    through the SQL-repair loop and burn the revision budget."""
    from wren_agent.tools.dolphin_scheduler import classify_execution_error
    from wren_agent.models.task import ErrorType

    log = (
        "2026-08-11 02:51:12 ERROR - execute failed "
        "org.springframework.dao.DataIntegrityViolationException: "
        "### Error updating database. Cause: org.postgresql.util.PSQLException: "
        'ERROR: null value in column "id" of relation '
        '"t_ds_task_instance_context" violates not-null constraint'
    )
    classification = classify_execution_error(
        task_instance_id="1", raw_log=log,
    )
    assert classification.error_type == ErrorType.CONFIG
    assert "t_ds_task_instance_context" in classification.summary


def test_classify_data_integrity_without_platform_markers_stays_data():
    """A real data integrity error (SQL constraint violation) without DS
    platform markers keeps the DATA classification."""
    from wren_agent.tools.dolphin_scheduler import classify_execution_error
    from wren_agent.models.task import ErrorType

    classification = classify_execution_error(
        task_instance_id="1",
        raw_log="ERROR: duplicate entry '1' for key 'PRIMARY'",
    )
    assert classification.error_type == ErrorType.DATA

def test_classify_parse_number_fail_is_logic():
    """Regression 55f47744: a Doris data-quality error (empty string in a
    date column breaking QUARTER('') / YEAR('') at execution) must classify as
    LOGIC — agent-repairable via design_revise with NULLIF/WHERE guards — not
    as RESOURCE/infra, which would skip the repair loop."""
    from wren_agent.tools.dolphin_scheduler import classify_execution_error
    from wren_agent.models.task import ErrorType

    log = (
        "execute sql error: errCode = 2, detailMessage = "
        "(127.0.0.1)[INVALID_ARGUMENT]parse number fail, string: ''"
    )
    classification = classify_execution_error(
        task_instance_id="1", raw_log=log,
    )
    assert classification.error_type == ErrorType.LOGIC
    assert "NULLIF" in classification.summary
