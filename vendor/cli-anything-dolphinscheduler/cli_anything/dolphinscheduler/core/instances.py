"""Workflow- and task-instance queries and controls for DolphinScheduler.

A *workflow instance* is one execution of a workflow definition; a *task
instance* is one execution of a single node inside it. This module wraps the
read paths (list/detail/tasks) plus the task-level controls (force-success,
stop) that agents reach for when a run needs a nudge.

Instances are addressed by numeric ``id`` (not the definition ``code``).
"""

from __future__ import annotations

from typing import Any, Optional

from .client import DolphinSchedulerClient


def _wf_instance_base(project_code: int) -> str:
    return f"/projects/{project_code}/workflow-instances"


def _task_instance_base(project_code: int) -> str:
    return f"/projects/{project_code}/task-instances"


# ── Workflow instances ───────────────────────────────────────────────────────


def list_workflow_instances(
    client: DolphinSchedulerClient,
    project_code: int,
    *,
    page_no: int = 1,
    page_size: int = 50,
    workflow_definition_code: Optional[int] = None,
    search_val: Optional[str] = None,
    state_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of workflow instances, optionally filtered.

    Args:
        state_type: A ``WorkflowExecutionStatus`` name (e.g. ``RUNNING_EXECUTION``,
            ``SUCCESS``, ``FAILURE``) to filter by run state.
        start_date/end_date: ``"yyyy-MM-dd HH:mm:ss"`` bounds on start time.
    """
    return client.get(
        _wf_instance_base(project_code),
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "workflowDefinitionCode": workflow_definition_code,
            "searchVal": search_val,
            "stateType": state_type,
            "startDate": start_date,
            "endDate": end_date,
        },
    )


def get_workflow_instance(
    client: DolphinSchedulerClient,
    project_code: int,
    instance_id: int,
) -> dict[str, Any]:
    """Fetch detail for a single workflow instance by id."""
    return client.get(f"{_wf_instance_base(project_code)}/{instance_id}")


def get_instance_tasks(
    client: DolphinSchedulerClient,
    project_code: int,
    instance_id: int,
) -> Any:
    """List the task instances belonging to one workflow instance."""
    return client.get(f"{_wf_instance_base(project_code)}/{instance_id}/tasks")


def delete_workflow_instance(
    client: DolphinSchedulerClient,
    project_code: int,
    instance_id: int,
) -> Any:
    """Delete a workflow instance by id."""
    return client.delete(f"{_wf_instance_base(project_code)}/{instance_id}")


# ── Task instances ───────────────────────────────────────────────────────────


def list_task_instances(
    client: DolphinSchedulerClient,
    project_code: int,
    *,
    page_no: int = 1,
    page_size: int = 50,
    workflow_instance_id: Optional[int] = None,
    workflow_instance_name: Optional[str] = None,
    workflow_definition_name: Optional[str] = None,
    task_name: Optional[str] = None,
    task_code: Optional[int] = None,
    executor_name: Optional[str] = None,
    state_type: Optional[str] = None,
    host: Optional[str] = None,
    search_val: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    task_execute_type: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of task instances, optionally scoped to a workflow run."""
    return client.get(
        _task_instance_base(project_code),
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "workflowInstanceId": workflow_instance_id,
            "workflowInstanceName": workflow_instance_name,
            "workflowDefinitionName": workflow_definition_name,
            "taskName": task_name,
            "taskCode": task_code,
            "executorName": executor_name,
            "stateType": state_type,
            "host": host,
            "searchVal": search_val,
            "startDate": start_date,
            "endDate": end_date,
            "taskExecuteType": task_execute_type,
        },
    )


def force_success_task(
    client: DolphinSchedulerClient,
    project_code: int,
    task_instance_id: int,
) -> Any:
    """Mark a failed task instance as successful so the run can proceed."""
    return client.post(
        f"{_task_instance_base(project_code)}/{task_instance_id}/force-success"
    )


def stop_task(
    client: DolphinSchedulerClient,
    project_code: int,
    task_instance_id: int,
) -> Any:
    """Request a stop of a single running task instance."""
    return client.post(
        f"{_task_instance_base(project_code)}/{task_instance_id}/stop"
    )
