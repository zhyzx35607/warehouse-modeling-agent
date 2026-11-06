"""Unit tests for cli-anything-dolphinscheduler core modules.

These tests run in isolation with no external dependencies (no network, no server).
All HTTP calls are mocked via unittest.mock.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.dolphinscheduler.core.config import (
    ClientConfig,
    load_config,
    save_config,
)
from cli_anything.dolphinscheduler.core.session import Session, load_session


# ── Config tests ─────────────────────────────────────────────────────────────


def test_load_config_cli_args_win():
    """CLI args override env vars and config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.json"
        cfg_file.write_text(json.dumps({"url": "http://file/ds", "token": "file-token"}))

        with mock.patch.dict(os.environ, {"DS_URL": "http://env/ds", "DS_TOKEN": "env-token"}):
            config = load_config(
                url="http://cli/ds",
                token="cli-token",
                config_file=cfg_file,
            )

        assert config.url == "http://cli/ds"
        assert config.token == "cli-token"


def test_load_config_env_over_file():
    """Env vars override config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.json"
        cfg_file.write_text(json.dumps({"url": "http://file/ds"}))

        with mock.patch.dict(os.environ, {"DS_TOKEN": "env-token"}, clear=True):
            config = load_config(config_file=cfg_file)

        assert config.url == "http://file/ds"
        assert config.token == "env-token"


def test_load_config_defaults():
    """With no overrides, load_config returns built-in defaults."""
    with mock.patch.dict(os.environ, {}, clear=True):
        config = load_config()

    assert config.url == "http://localhost:12345/dolphinscheduler"
    assert config.timeout == 30.0
    assert config.verify_tls is True
    assert config.token is None


def test_save_config():
    """save_config persists config to JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.json"
        config = ClientConfig(
            url="http://test/ds",
            token="test-token",
            user="testuser",
            password="testpass",
        )

        path = save_config(config, config_file=cfg_file)

        assert path == cfg_file
        assert cfg_file.exists()
        data = json.loads(cfg_file.read_text())
        assert data["url"] == "http://test/ds"
        assert data["token"] == "test-token"
        assert data["user"] == "testuser"
        assert data["password"] == "testpass"


def test_config_redacted():
    """config.redacted() masks secrets."""
    config = ClientConfig(token="abcd1234", password="secret123")
    redacted = config.redacted()

    assert redacted["token"] == "ab****34"
    assert redacted["password"] == "se****23"


# ── Session tests ────────────────────────────────────────────────────────────


def test_load_session_missing():
    """load_session returns a fresh session when file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "nonexistent.json"
        session = load_session(path)

    assert session.project_code is None
    assert session.project_name is None


def test_session_select_project():
    """select_project updates session state."""
    session = Session()
    session.select_project(12345, "TestProject")

    assert session.project_code == 12345
    assert session.project_name == "TestProject"
    assert session.has_project is True


def test_session_require_project_raises():
    """require_project raises when no project is selected."""
    session = Session()

    with pytest.raises(ValueError, match="No project selected"):
        session.require_project()


def test_session_save_and_load():
    """Session can be persisted and loaded back."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "session.json"

        session1 = Session(path=path)
        session1.select_project(99, "Demo")
        session1.save()

        session2 = load_session(path)
        assert session2.project_code == 99
        assert session2.project_name == "Demo"


def test_session_clear_project():
    """clear_project resets project state."""
    session = Session()
    session.select_project(123, "Test")
    session.clear_project()

    assert session.project_code is None
    assert session.project_name is None
    assert session.has_project is False


# ── Client tests ─────────────────────────────────────────────────────────────


def test_client_request_success():
    """Client unwraps success envelope and returns data field."""
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 0, "msg": "success", "data": {"foo": "bar"}}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        result = client.get("/test")

    assert result == {"foo": "bar"}


def test_client_request_api_error():
    """Client raises APIError on non-zero code."""
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig
    from cli_anything.dolphinscheduler.core.errors import APIError

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 10001, "msg": "Project not found"}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        with pytest.raises(APIError, match="Project not found"):
            client.get("/test")


def test_client_request_http_401():
    """Client raises AuthError on HTTP 401."""
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig
    from cli_anything.dolphinscheduler.core.errors import AuthError

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    mock_response = mock.Mock()
    mock_response.status_code = 401

    with mock.patch.object(client._session, "request", return_value=mock_response):
        with pytest.raises(AuthError, match="Authentication failed"):
            client.get("/test")


def test_client_token_header_set():
    """Client sets token header when config has a token."""
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    assert client._session.headers["token"] == "test-token"


# ── Projects tests ───────────────────────────────────────────────────────────


def test_resolve_project_code_by_code():
    """resolve_project_code returns numeric code as-is."""
    from cli_anything.dolphinscheduler.core import projects
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    code = projects.resolve_project_code(client, "123")
    assert code == 123


def test_resolve_project_code_by_name():
    """resolve_project_code looks up name and returns code."""
    from cli_anything.dolphinscheduler.core import projects
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {
        "code": 0,
        "data": [{"code": 456, "name": "Demo"}],
    }

    with mock.patch.object(client._session, "request", return_value=mock_response):
        code = projects.resolve_project_code(client, "Demo")

    assert code == 456


def test_get_project_by_name_not_found():
    """get_project_by_name raises NotFoundError when name doesn't exist."""
    from cli_anything.dolphinscheduler.core import projects
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig
    from cli_anything.dolphinscheduler.core.errors import NotFoundError

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 0, "data": []}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        with pytest.raises(NotFoundError, match="No project named"):
            projects.get_project_by_name(client, "Missing")


# ── Workflows tests ──────────────────────────────────────────────────────────


def test_dag_builder_single_task():
    """DagBuilder with one task produces correct JSON."""
    from cli_anything.dolphinscheduler.core.workflows import DagBuilder
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)
    builder = DagBuilder(client, 100)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 0, "data": [1001]}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        builder.add_shell("extract", "python extract.py")
        task_json, relation_json = builder.build()

    tasks = json.loads(task_json)
    relations = json.loads(relation_json)

    assert len(tasks) == 1
    assert tasks[0]["code"] == 1001
    assert tasks[0]["name"] == "extract"

    assert len(relations) == 1
    assert relations[0]["preTaskCode"] == 0
    assert relations[0]["postTaskCode"] == 1001


def test_dag_builder_linear_dag():
    """DagBuilder with linear dependencies produces correct relations."""
    from cli_anything.dolphinscheduler.core.workflows import DagBuilder
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)
    builder = DagBuilder(client, 100)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 0, "data": [1001, 1002, 1003]}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        builder.add_shell("extract", "echo extract")
        builder.add_shell("transform", "echo transform", depends_on=["extract"])
        builder.add_shell("load", "echo load", depends_on=["transform"])
        task_json, relation_json = builder.build()

    relations = json.loads(relation_json)
    assert len(relations) == 3

    # Extract relation edges as (pre, post) tuples
    edges = [(r["preTaskCode"], r["postTaskCode"]) for r in relations]
    assert (0, 1001) in edges  # extract is root
    assert (1001, 1002) in edges  # extract → transform
    assert (1002, 1003) in edges  # transform → load


def test_dag_builder_unknown_dep_raises():
    """DagBuilder raises ValueError for unknown dependency."""
    from cli_anything.dolphinscheduler.core.workflows import DagBuilder
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    config = ClientConfig(url="http://test/ds", token="test-token")
    client = DolphinSchedulerClient(config)
    builder = DagBuilder(client, 100)

    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"code": 0, "data": [1001]}

    with mock.patch.object(client._session, "request", return_value=mock_response):
        builder.add_shell("load", "echo load", depends_on=["missing"])

        with pytest.raises(ValueError, match="depends on unknown task"):
            builder.build()


# ── Task construction tests ─────────────────────────────────────────────────


def test_shell_task_definition_shape():
    """ShellTask renders the taskDefinitionJson shape accepted by the API."""
    from cli_anything.dolphinscheduler.core.tasks import ShellTask, dumps_task_definitions

    task = ShellTask(
        name="extract",
        script="echo extract",
        code=1001,
        depends_on=["start"],
        description="Extract data",
        worker_group="default",
        task_priority="HIGH",
        fail_retry_times=2,
        fail_retry_interval=3,
        timeout=60,
    )

    definition = task.to_definition()
    assert definition["code"] == 1001
    assert definition["name"] == "extract"
    assert definition["taskType"] == "SHELL"
    assert definition["taskParams"]["rawScript"] == "echo extract"
    assert definition["taskParams"]["resourceList"] == []
    assert definition["taskParams"]["localParams"] == []
    assert definition["taskParams"]["dependence"] == {}
    assert definition["taskParams"]["conditionResult"] == {"successNode": [], "failedNode": []}
    assert definition["taskPriority"] == "HIGH"
    assert definition["failRetryTimes"] == 2
    assert definition["failRetryInterval"] == 3
    assert definition["timeout"] == 60

    payload = json.loads(dumps_task_definitions([task]))
    assert payload == [definition]


def test_shell_task_validation():
    """ShellTask rejects invalid task construction inputs before API calls."""
    from cli_anything.dolphinscheduler.core.tasks import ShellTask

    with pytest.raises(ValueError, match="name must not be empty"):
        ShellTask(name="", script="echo ok")

    with pytest.raises(ValueError, match="script must not be empty"):
        ShellTask(name="extract", script="")

    with pytest.raises(ValueError, match="Invalid task_priority"):
        ShellTask(name="extract", script="echo ok", task_priority="INVALID")

    with pytest.raises(ValueError, match="code must be a positive integer"):
        ShellTask(name="extract", script="echo ok", code=0)


def test_generic_task_definition_shape():
    """TaskDefinition supports arbitrary DolphinScheduler task types."""
    from cli_anything.dolphinscheduler.core.tasks import TaskDefinition

    task = TaskDefinition(
        name="spark_job",
        task_type="SPARK",
        code=2001,
        task_params={"mainClass": "org.example.Job", "mainJar": {"id": 1}},
    )

    definition = task.to_definition()
    assert definition["taskType"] == "SPARK"
    assert definition["taskParams"]["mainClass"] == "org.example.Job"
    assert definition["taskParams"]["mainJar"] == {"id": 1}
    assert definition["taskParams"]["localParams"] == []
    assert definition["taskParams"]["conditionResult"] == {"successNode": [], "failedNode": []}


def test_python_sql_http_task_builders():
    """Convenience builders cover high-frequency non-SHELL task types."""
    from cli_anything.dolphinscheduler.core import tasks

    python_task = tasks.build_python_task(name="py", script="print('ok')", code=3001)
    assert python_task.to_definition()["taskType"] == "PYTHON"
    assert python_task.to_definition()["taskParams"]["rawScript"] == "print('ok')"

    sql_task = tasks.build_sql_task(
        name="query",
        sql="select 1",
        datasource=10,
        code=3002,
        sql_type="0",
    )
    sql_definition = sql_task.to_definition()
    assert sql_definition["taskType"] == "SQL"
    assert sql_definition["taskParams"]["datasource"] == 10
    assert sql_definition["taskParams"]["sql"] == "select 1"

    http_task = tasks.build_http_task(
        name="probe",
        url="https://example.com/health",
        code=3003,
        method="POST",
        body='{"ok": true}',
    )
    http_definition = http_task.to_definition()
    assert http_definition["taskType"] == "HTTP"
    assert http_definition["taskParams"]["httpMethod"] == "POST"
    assert http_definition["taskParams"]["url"] == "https://example.com/health"


def test_cli_task_build_shell_json_with_explicit_code():
    """CLI can build a SHELL taskDefinitionJson entry without server access."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--url",
            "http://test/ds",
            "--token",
            "test-token",
            "--json",
            "task",
            "build-shell",
            "--name",
            "extract",
            "--script",
            "echo extract",
            "--code",
            "1001",
            "--depends-on",
            "start",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    task_definition = json.loads(data["taskDefinitionJson"])[0]
    assert task_definition["code"] == 1001
    assert task_definition["taskType"] == "SHELL"
    assert task_definition["taskParams"]["rawScript"] == "echo extract"
    assert data["depends_on"] == ["start"]


def test_cli_task_build_generic_json_with_explicit_code():
    """CLI can build arbitrary taskDefinitionJson from taskType + params JSON."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--url",
            "http://test/ds",
            "--token",
            "test-token",
            "--json",
            "task",
            "build-generic",
            "--name",
            "spark_job",
            "--task-type",
            "SPARK",
            "--params-json",
            '{"mainClass":"org.example.Job"}',
            "--code",
            "2001",
        ],
    )

    assert result.exit_code == 0, result.output
    task_definition = json.loads(json.loads(result.output)["data"]["taskDefinitionJson"])[0]
    assert task_definition["taskType"] == "SPARK"
    assert task_definition["taskParams"]["mainClass"] == "org.example.Job"


def test_cli_task_build_sql_json_with_explicit_code():
    """CLI can build a SQL taskDefinitionJson entry."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--url",
            "http://test/ds",
            "--token",
            "test-token",
            "--json",
            "task",
            "build-sql",
            "--name",
            "query",
            "--sql",
            "select 1",
            "--datasource",
            "10",
            "--code",
            "3002",
        ],
    )

    assert result.exit_code == 0, result.output
    task_definition = json.loads(json.loads(result.output)["data"]["taskDefinitionJson"])[0]
    assert task_definition["taskType"] == "SQL"
    assert task_definition["taskParams"]["datasource"] == 10
    assert task_definition["taskParams"]["sql"] == "select 1"


def test_cli_task_build_http_json_with_explicit_code():
    """CLI can build an HTTP taskDefinitionJson entry without closure shadowing."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--url",
            "http://test/ds",
            "--token",
            "test-token",
            "--json",
            "task",
            "build-http",
            "--name",
            "health",
            "--url",
            "https://example.com/health",
            "--method",
            "POST",
            "--body",
            '{"ok":true}',
            "--code",
            "3003",
        ],
    )

    assert result.exit_code == 0, result.output
    task_definition = json.loads(json.loads(result.output)["data"]["taskDefinitionJson"])[0]
    assert task_definition["taskType"] == "HTTP"
    assert task_definition["taskParams"]["httpMethod"] == "POST"
    assert task_definition["taskParams"]["httpBody"] == '{"ok":true}'
    assert task_definition["taskParams"]["url"] == "https://example.com/health"


# ── Instances tests ─────────────────────────────────────────────────────────


def _success_response(data):
    response = mock.Mock()
    response.status_code = 200
    response.headers = {"Content-Type": "application/json"}
    response.json.return_value = {"code": 0, "data": data}
    return response


def test_get_instance_tasks():
    """get_instance_tasks calls the workflow-instance task-list endpoint."""
    from cli_anything.dolphinscheduler.core import instances
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))
    payload = {"workflowInstanceState": "SUCCESS", "taskList": [{"id": 10}]}

    with mock.patch.object(client._session, "request", return_value=_success_response(payload)) as request:
        result = instances.get_instance_tasks(client, 100, 555)

    assert result == payload
    assert request.call_args.args[:2] == ("GET", "http://test/ds/projects/100/workflow-instances/555/tasks")


def test_list_task_instances_with_filters():
    """list_task_instances passes all supported filter params."""
    from cli_anything.dolphinscheduler.core import instances
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))
    payload = {"totalList": [{"id": 10, "name": "load"}]}

    with mock.patch.object(client._session, "request", return_value=_success_response(payload)) as request:
        result = instances.list_task_instances(
            client,
            100,
            page_no=2,
            page_size=25,
            workflow_instance_id=555,
            workflow_instance_name="run-1",
            workflow_definition_name="ETL",
            task_name="load",
            task_code=9001,
            executor_name="admin",
            state_type="SUCCESS",
            host="worker:1234",
            search_val="lo",
            start_date="2026-07-08 00:00:00",
            end_date="2026-07-08 23:59:59",
            task_execute_type="BATCH",
        )

    assert result == payload
    assert request.call_args.args[:2] == ("GET", "http://test/ds/projects/100/task-instances")
    params = request.call_args.kwargs["params"]
    assert params == {
        "pageNo": 2,
        "pageSize": 25,
        "workflowInstanceId": 555,
        "workflowInstanceName": "run-1",
        "workflowDefinitionName": "ETL",
        "taskName": "load",
        "taskCode": 9001,
        "executorName": "admin",
        "stateType": "SUCCESS",
        "host": "worker:1234",
        "searchVal": "lo",
        "startDate": "2026-07-08 00:00:00",
        "endDate": "2026-07-08 23:59:59",
        "taskExecuteType": "BATCH",
    }


def test_task_instance_controls():
    """Task control wrappers call the real task-instance endpoints."""
    from cli_anything.dolphinscheduler.core import instances
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))

    with mock.patch.object(client._session, "request", return_value=_success_response(None)) as request:
        instances.force_success_task(client, 100, 10)
        instances.stop_task(client, 100, 11)
        instances.delete_workflow_instance(client, 100, 555)

    called_urls = [call.args[1] for call in request.call_args_list]
    assert "http://test/ds/projects/100/task-instances/10/force-success" in called_urls
    assert "http://test/ds/projects/100/task-instances/11/stop" in called_urls
    assert "http://test/ds/projects/100/workflow-instances/555" in called_urls


def test_cli_instance_task_list_json():
    """CLI task-list command returns the service payload in JSON mode."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    payload = {"totalList": [{"id": 10, "name": "load", "state": "SUCCESS"}]}
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.instances.list_task_instances",
        return_value=payload,
    ) as list_task_instances:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--project-code",
                "100",
                "--json",
                "instance",
                "task-list",
                "--workflow-instance-id",
                "555",
                "--state",
                "SUCCESS",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == payload
    assert list_task_instances.call_args.args[1] == 100
    assert list_task_instances.call_args.kwargs["workflow_instance_id"] == 555
    assert list_task_instances.call_args.kwargs["state_type"] == "SUCCESS"


def test_cli_instance_tasks_json():
    """CLI tasks command exposes workflow-instance task details."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    payload = {
        "workflowInstanceState": "SUCCESS",
        "taskList": [{"id": 10, "name": "extract", "state": "SUCCESS"}],
    }
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.instances.get_instance_tasks",
        return_value=payload,
    ) as get_instance_tasks:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--project-code",
                "100",
                "--json",
                "instance",
                "tasks",
                "555",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == payload
    assert get_instance_tasks.call_args.args[1:] == (100, 555)


# ── Resource Center tests ───────────────────────────────────────────────────


def test_client_download_binary_success():
    """Client.download returns binary payloads without JSON parsing."""
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))
    response = mock.Mock()
    response.status_code = 200
    response.headers = {"Content-Type": "application/octet-stream"}
    response.content = b"resource-bytes"

    with mock.patch.object(client._session, "request", return_value=response) as request:
        result = client.download("/resources/download", params={"fullName": "/tenant/demo.py"})

    assert result == b"resource-bytes"
    assert request.call_args.args[:2] == ("GET", "http://test/ds/resources/download")
    assert request.call_args.kwargs["params"] == {"fullName": "/tenant/demo.py"}


def test_resource_create_file_from_content_splits_name():
    """Resource content creation passes fileName/suffix expected by the API."""
    from cli_anything.dolphinscheduler.core import resources
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))

    with mock.patch.object(client._session, "request", return_value=_success_response(None)) as request:
        resources.create_file_from_content(
            client,
            "job.py",
            "print('ok')",
            "/tenant/resources",
        )

    assert request.call_args.args[:2] == ("POST", "http://test/ds/resources/online-create")
    assert request.call_args.kwargs["data"] == {
        "type": "FILE",
        "fileName": "job",
        "suffix": "py",
        "content": "print('ok')",
        "currentDir": "/tenant/resources",
    }


def test_resource_list_items_params():
    """Resource directory listing passes pagination and search params."""
    from cli_anything.dolphinscheduler.core import resources
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))
    payload = {"totalList": [{"fileName": "job.py"}]}

    with mock.patch.object(client._session, "request", return_value=_success_response(payload)) as request:
        result = resources.list_items(
            client,
            "/tenant/resources",
            search_val="job",
            page_no=2,
            page_size=5,
        )

    assert result == payload
    assert request.call_args.args[:2] == ("GET", "http://test/ds/resources")
    assert request.call_args.kwargs["params"] == {
        "type": "FILE",
        "fullName": "/tenant/resources",
        "searchVal": "job",
        "pageNo": 2,
        "pageSize": 5,
    }


def test_resource_upload_file_uses_multipart(tmp_path):
    """Resource upload sends the file and form fields expected by the API."""
    from cli_anything.dolphinscheduler.core import resources
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    local_file = tmp_path / "job.py"
    local_file.write_text("print('ok')", encoding="utf-8")
    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))

    with mock.patch.object(client._session, "request", return_value=_success_response(None)) as request:
        resources.upload_file(client, str(local_file), "/tenant/resources")

    assert request.call_args.args[:2] == ("POST", "http://test/ds/resources")
    assert request.call_args.kwargs["data"] == {
        "type": "FILE",
        "name": "job.py",
        "currentDir": "/tenant/resources",
    }
    assert request.call_args.kwargs["files"]["file"][0] == "job.py"


def test_cli_resource_create_file_json(tmp_path):
    """CLI can create a Resource Center file from a local content file."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    content_file = tmp_path / "job.py"
    content_file.write_text("print('ok')", encoding="utf-8")
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.resources.create_file_from_content",
        return_value=None,
    ) as create_file:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "resource",
                "create-file",
                "--name",
                "job.py",
                "--current-dir",
                "/tenant/resources",
                "--content-file",
                str(content_file),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["name"] == "job.py"
    assert create_file.call_args.args[1:] == ("job.py", "print('ok')", "/tenant/resources")


def test_cli_resource_download_writes_file(tmp_path):
    """CLI resource download writes bytes to the requested local output path."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    output = tmp_path / "job.py"
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.resources.download_resource",
        return_value=b"print('ok')",
    ) as download:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "resource",
                "download",
                "/tenant/resources/job.py",
                "--output",
                str(output),
            ],
        )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"print('ok')"
    assert json.loads(result.output)["data"]["bytes"] == len(b"print('ok')")
    assert download.call_args.args[1] == "/tenant/resources/job.py"


def test_cli_resource_update_content_requires_one_source():
    """CLI rejects ambiguous Resource Center content input."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--url",
            "http://test/ds",
            "--token",
            "test-token",
            "--json",
            "resource",
            "update-content",
            "/tenant/resources/job.py",
        ],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["success"] is False
    assert error["error"] == "invalid_input"
    assert "pass --content or --content-file" in error["message"]


# ── Datasource, schedule, run, and log tests ────────────────────────────────


def test_datasource_create_uses_json_body():
    """Datasource creation sends the native datasource JSON as request body."""
    from cli_anything.dolphinscheduler.core import datasources
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    payload = {
        "type": "MYSQL",
        "name": "agent_mysql",
        "host": "localhost",
        "port": 3306,
        "userName": "root",
        "password": "secret",
        "database": "dolphinscheduler",
    }
    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))

    with mock.patch.object(client._session, "request", return_value=_success_response({"id": 9})) as request:
        result = datasources.create_datasource(client, payload)

    assert result == {"id": 9}
    assert request.call_args.args[:2] == ("POST", "http://test/ds/datasources")
    assert request.call_args.kwargs["json"] == payload


def test_datasource_metadata_params():
    """Datasource metadata helpers pass datasource/database/table query params."""
    from cli_anything.dolphinscheduler.core import datasources
    from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
    from cli_anything.dolphinscheduler.core.config import ClientConfig

    client = DolphinSchedulerClient(ClientConfig(url="http://test/ds", token="test-token"))

    with mock.patch.object(client._session, "request", return_value=_success_response([])) as request:
        datasources.list_table_columns(client, 9, "warehouse", "orders")

    assert request.call_args.args[:2] == ("GET", "http://test/ds/datasources/tableColumns")
    assert request.call_args.kwargs["params"] == {
        "datasourceId": 9,
        "database": "warehouse",
        "tableName": "orders",
    }


def test_cli_datasource_create_json():
    """CLI datasource create accepts native datasource JSON."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    payload = '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306}'
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.datasources.create_datasource",
        return_value={"id": 9, "name": "agent_mysql"},
    ) as create_datasource:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "datasource",
                "create",
                "--param-json",
                payload,
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["id"] == 9
    assert create_datasource.call_args.args[1]["type"] == "MYSQL"


def test_cli_project_update_json():
    """CLI project update exposes the existing project update wrapper."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.projects.resolve_project_code",
        return_value=100,
    ) as resolve_project_code, mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.projects.update_project",
        return_value={"code": 100, "name": "new_name"},
    ) as update_project:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "project",
                "update",
                "old_name",
                "--name",
                "new_name",
                "--description",
                "updated",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["name"] == "new_name"
    assert resolve_project_code.call_args.args[1] == "old_name"
    assert update_project.call_args.args[1:] == (100, "new_name", "updated")


def test_cli_run_backfill_json():
    """CLI run backfill exposes complement-data execution."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.workflows.resolve_workflow_code",
        return_value=9001,
    ) as resolve_workflow_code, mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.executors.backfill_workflow",
        return_value=[101, 102],
    ) as backfill_workflow:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--project-code",
                "100",
                "--json",
                "run",
                "backfill",
                "daily_etl",
                "--start-date",
                "2026-07-01 00:00:00",
                "--end-date",
                "2026-07-02 00:00:00",
                "--run-mode",
                "RUN_MODE_PARALLEL",
                "--expected-parallelism-number",
                "2",
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["instance_ids"] == [101, 102]
    assert resolve_workflow_code.call_args.args[1:] == (100, "daily_etl")
    assert backfill_workflow.call_args.args[1:5] == (
        100,
        9001,
        "2026-07-01 00:00:00",
        "2026-07-02 00:00:00",
    )
    assert backfill_workflow.call_args.kwargs["run_mode"] == "RUN_MODE_PARALLEL"


def test_cli_schedule_preview_json():
    """CLI schedule preview calls the server preview endpoint without mutation."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.schedules.preview_schedule",
        return_value=["2026-07-09 01:00:00"],
    ) as preview_schedule:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--project-code",
                "100",
                "--json",
                "schedule",
                "preview",
                "--crontab",
                "0 0 1 * * ? *",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == ["2026-07-09 01:00:00"]
    assert preview_schedule.call_args.args[1:3] == (100, "0 0 1 * * ? *")


def test_cli_token_generate_json():
    """CLI token generate exposes non-persisted token generation."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.tokens.generate_token_string",
        return_value="generated-token",
    ) as generate_token_string:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "token",
                "generate",
                "--user-id",
                "1",
                "--expire-time",
                "2030-01-01 00:00:00",
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["token"] == "generated-token"
    assert generate_token_string.call_args.args[1:] == (1, "2030-01-01 00:00:00")


def test_cli_log_download_writes_file(tmp_path):
    """CLI log download writes task-instance log bytes to disk."""
    from cli_anything.dolphinscheduler.dolphinscheduler_cli import cli

    output = tmp_path / "task.log"
    runner = CliRunner()

    with mock.patch(
        "cli_anything.dolphinscheduler.dolphinscheduler_cli.logs.download_task_log",
        return_value=b"task log",
    ) as download_task_log:
        result = runner.invoke(
            cli,
            [
                "--url",
                "http://test/ds",
                "--token",
                "test-token",
                "--json",
                "log",
                "download",
                "88",
                "--output",
                str(output),
            ],
        )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"task log"
    assert json.loads(result.output)["data"]["bytes"] == len(b"task log")
    assert download_task_log.call_args.args[1] == 88
