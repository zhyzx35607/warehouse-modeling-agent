"""End-to-end tests against a real DolphinScheduler server.

These tests require:
1. A running DolphinScheduler instance (Docker Compose / standalone)
2. Admin credentials (default: admin / dolphinscheduler123)
3. Network access to the API server

Run with:
    pytest -v tests/test_full_e2e.py

Skip if server unavailable:
    pytest -v tests/test_full_e2e.py -m "not e2e"
"""

import time

import pytest

from cli_anything.dolphinscheduler.core import (
    executors,
    instances,
    projects,
    workflows,
)
from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
from cli_anything.dolphinscheduler.core.config import ClientConfig

# Mark all tests in this file as e2e
pytestmark = pytest.mark.e2e

API_URL = "http://localhost:12345/dolphinscheduler"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "dolphinscheduler123"


@pytest.fixture(scope="module")
def client():
    """Create an authenticated client for E2E tests."""
    config = ClientConfig(url=API_URL, user=ADMIN_USER, password=ADMIN_PASSWORD)
    client = DolphinSchedulerClient(config)

    # Verify connectivity
    try:
        client.login(ADMIN_USER, ADMIN_PASSWORD)
    except Exception as exc:
        pytest.skip(f"DolphinScheduler server not reachable: {exc}")

    yield client
    client.close()


def test_full_workflow_lifecycle(client):
    """Create project → workflow → release → run → verify → cleanup."""
    project_name = "E2E_Test_Project"

    # 1. Create project
    project_data = projects.create_project(client, project_name, description="E2E test")
    project_code = int(project_data["code"])
    assert project_code > 0

    try:
        # 2. Verify project exists
        all_projects = projects.list_all_projects(client)
        assert any(p.get("code") == project_code for p in all_projects)

        # 3. Create workflow with 2 SHELL tasks
        builder = workflows.DagBuilder(client, project_code)
        builder.add_shell("task1", "echo 'Task 1 complete'")
        builder.add_shell("task2", "echo 'Task 2 complete'", depends_on=["task1"])
        task_json, relation_json = builder.build()

        wf_data = workflows.create_workflow(
            client,
            project_code,
            "E2E_Test_Workflow",
            task_json,
            relation_json,
            description="E2E test workflow",
        )
        workflow_code = int(wf_data["code"])

        # 4. Release workflow ONLINE
        workflows.release_workflow(client, project_code, workflow_code, online=True)

        # 5. Trigger workflow
        instance_ids = executors.start_workflow(client, project_code, workflow_code)
        assert len(instance_ids) == 1
        instance_id = instance_ids[0]

        # 6. Poll instance state (up to 60s)
        max_wait = 60
        poll_interval = 2
        final_state = None

        for _ in range(max_wait // poll_interval):
            detail = instances.get_workflow_instance(client, project_code, instance_id)
            state = detail.get("state") if isinstance(detail, dict) else None
            if state in ("SUCCESS", "FAILURE", "STOP"):
                final_state = state
                break
            time.sleep(poll_interval)

        assert final_state == "SUCCESS", f"Workflow did not succeed: {final_state}"

        # 7. Query task instances
        task_detail = instances.get_instance_tasks(client, project_code, instance_id)
        task_list = task_detail.get("taskList", []) if isinstance(task_detail, dict) else []
        assert len(task_list) == 2
        assert all(t.get("state") == "SUCCESS" for t in task_list)

        # 8. Take the workflow OFFLINE, then delete it. DolphinScheduler
        #    refuses to delete an ONLINE definition, so the order matters.
        workflows.release_workflow(client, project_code, workflow_code, online=False)
        workflows.delete_workflow(client, project_code, workflow_code)

    finally:
        # 9. Delete the project. A project cannot be deleted while it still has
        #    workflow definitions, so ensure any leftovers are removed first.
        _safe_cleanup_project(client, project_code)


def _safe_cleanup_project(client, project_code: int) -> None:
    """Best-effort teardown: offline+delete any workflows, then the project."""
    try:
        for entry in workflows.list_all_workflows(client, project_code):
            wf = entry.get("workflowDefinition", entry)
            code = wf.get("code")
            if code is None:
                continue
            try:
                workflows.release_workflow(client, project_code, int(code), online=False)
            except Exception:
                pass  # already offline
            try:
                workflows.delete_workflow(client, project_code, int(code))
            except Exception:
                pass
    except Exception:
        pass
    projects.delete_project(client, project_code)


def test_schedule_preview_only(client):
    """Preview schedule without creating it (lightweight E2E check)."""
    # Create a minimal project
    project_data = projects.create_project(client, "E2E_Schedule_Test")
    project_code = int(project_data["code"])

    try:
        # Preview a cron expression
        from cli_anything.dolphinscheduler.core import schedules

        result = schedules.preview_schedule(client, project_code, "0 0 1 * * ? *")
        # Result should be a list of future fire times
        assert isinstance(result, list) or isinstance(result, str)

    finally:
        projects.delete_project(client, project_code)
