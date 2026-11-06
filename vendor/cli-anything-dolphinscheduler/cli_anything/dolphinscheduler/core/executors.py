"""Execution control for DolphinScheduler workflows.

This module wraps the ``executors`` controller — the endpoints that actually
*run* things. It covers three concerns:

* **Triggering** a workflow definition into a new workflow instance.
* **Controlling** a running instance (stop, pause, rerun, recover ...).
* **Backfill** (complement) runs across a date range.

The server binds these as form parameters and returns the created instance IDs
for a trigger, or an empty envelope for control actions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .client import DolphinSchedulerClient

# Enum values accepted by the server. Kept here as the single source of truth so
# the CLI layer can validate before making a round trip.
FAILURE_STRATEGIES = ("CONTINUE", "END")
WARNING_TYPES = ("NONE", "SUCCESS", "FAILURE", "ALL")
PRIORITIES = ("HIGHEST", "HIGH", "MEDIUM", "LOW", "LOWEST")
RUN_MODES = ("RUN_MODE_SERIAL", "RUN_MODE_PARALLEL")
EXECUTE_TYPES = (
    "REPEAT_RUNNING",
    "RECOVER_SUSPENDED_PROCESS",
    "START_FAILURE_TASK_PROCESS",
    "STOP",
    "PAUSE",
)


def _executor_base(project_code: int) -> str:
    return f"/projects/{project_code}/executors"


def start_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    workflow_definition_code: int,
    *,
    schedule_time: Optional[str] = None,
    failure_strategy: str = "CONTINUE",
    warning_type: str = "NONE",
    workflow_instance_priority: str = "MEDIUM",
    worker_group: str = "default",
    tenant_code: str = "default",
    environment_code: int = -1,
    warning_group_id: Optional[int] = None,
    start_params: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    exec_type: str = "START_PROCESS",
) -> list[int]:
    """Trigger a single run of a workflow definition.

    Args:
        workflow_definition_code: The workflow to run (must be ONLINE).
        schedule_time: Nominal schedule time. For a plain run this can be a
            single ``"yyyy-MM-dd HH:mm:ss"`` timestamp; defaults to an empty
            string, which the server interprets as "now".
        start_params: Optional global parameter overrides for this run.
        dry_run: When True, the server validates without executing tasks.

    Returns:
        The list of created workflow-instance IDs (usually one).
    """
    _validate_choice("failure_strategy", failure_strategy, FAILURE_STRATEGIES)
    _validate_choice("warning_type", warning_type, WARNING_TYPES)
    _validate_choice("workflow_instance_priority", workflow_instance_priority, PRIORITIES)

    data = {
        "workflowDefinitionCode": workflow_definition_code,
        "scheduleTime": schedule_time or "",
        "failureStrategy": failure_strategy,
        "warningType": warning_type,
        "workflowInstancePriority": workflow_instance_priority,
        "workerGroup": worker_group,
        "tenantCode": tenant_code,
        "environmentCode": environment_code,
        "warningGroupId": warning_group_id,
        "execType": exec_type,
        "dryRun": 1 if dry_run else 0,
    }
    if start_params is not None:
        data["startParams"] = json.dumps(start_params)

    result = client.post(
        f"{_executor_base(project_code)}/start-workflow-instance",
        data=data,
    )
    return [int(i) for i in (result or [])]


def backfill_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    workflow_definition_code: int,
    start_date: str,
    end_date: str,
    *,
    failure_strategy: str = "CONTINUE",
    warning_type: str = "NONE",
    workflow_instance_priority: str = "MEDIUM",
    worker_group: str = "default",
    tenant_code: str = "default",
    environment_code: int = -1,
    run_mode: Optional[str] = None,
    expected_parallelism_number: Optional[int] = None,
    dry_run: bool = False,
) -> list[int]:
    """Run a workflow across a historical date range (complement data).

    ``start_date``/``end_date`` are ``"yyyy-MM-dd HH:mm:ss"`` strings; they are
    packed into the JSON ``scheduleTime`` structure the server expects for
    ``COMPLEMENT_DATA`` runs.
    """
    _validate_choice("failure_strategy", failure_strategy, FAILURE_STRATEGIES)
    _validate_choice("warning_type", warning_type, WARNING_TYPES)
    _validate_choice("workflow_instance_priority", workflow_instance_priority, PRIORITIES)

    schedule_time = json.dumps(
        {"complementStartDate": start_date, "complementEndDate": end_date}
    )
    data = {
        "workflowDefinitionCode": workflow_definition_code,
        "scheduleTime": schedule_time,
        "failureStrategy": failure_strategy,
        "warningType": warning_type,
        "workflowInstancePriority": workflow_instance_priority,
        "workerGroup": worker_group,
        "tenantCode": tenant_code,
        "environmentCode": environment_code,
        "execType": "COMPLEMENT_DATA",
        "runMode": run_mode,
        "expectedParallelismNumber": expected_parallelism_number,
        "dryRun": 1 if dry_run else 0,
    }
    result = client.post(
        f"{_executor_base(project_code)}/start-workflow-instance",
        data=data,
    )
    return [int(i) for i in (result or [])]


def control_instance(
    client: DolphinSchedulerClient,
    project_code: int,
    workflow_instance_id: int,
    execute_type: str,
) -> Any:
    """Apply a control action to a running/finished workflow instance.

    ``execute_type`` is one of :data:`EXECUTE_TYPES` — e.g. ``STOP``, ``PAUSE``,
    ``REPEAT_RUNNING`` (rerun), ``RECOVER_SUSPENDED_PROCESS`` (resume a paused
    run), or ``START_FAILURE_TASK_PROCESS`` (retry only failed tasks).
    """
    _validate_choice("execute_type", execute_type, EXECUTE_TYPES)
    return client.post(
        f"{_executor_base(project_code)}/execute",
        data={
            "workflowInstanceId": workflow_instance_id,
            "executeType": execute_type,
        },
    )


def _validate_choice(field_name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(
            f"Invalid {field_name}={value!r}; expected one of {', '.join(allowed)}"
        )
