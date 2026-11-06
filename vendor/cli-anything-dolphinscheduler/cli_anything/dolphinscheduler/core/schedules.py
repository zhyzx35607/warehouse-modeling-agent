"""Cron-schedule operations for DolphinScheduler workflows.

A *schedule* attaches a crontab to a workflow definition so the server runs it
automatically. The lifecycle is: create (offline) → online. Nothing fires until
the schedule is brought online.

The server takes the timing details as a single JSON ``schedule`` form field
with ``startTime``, ``endTime``, ``crontab``, and ``timezoneId``. This module
builds that object for you and wraps the online/offline lifecycle.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .client import DolphinSchedulerClient

DEFAULT_TIMEZONE = "Asia/Shanghai"
# Far-future default so a schedule stays active unless the caller sets bounds.
_DEFAULT_START = "2020-01-01 00:00:00"
_DEFAULT_END = "2100-01-01 00:00:00"
DEFAULT_START_TIME = _DEFAULT_START
DEFAULT_END_TIME = _DEFAULT_END


def _schedule_base(project_code: int) -> str:
    return f"/projects/{project_code}/schedules"


def build_schedule_payload(
    crontab: str,
    *,
    start_time: str = _DEFAULT_START,
    end_time: str = _DEFAULT_END,
    timezone_id: str = DEFAULT_TIMEZONE,
) -> str:
    """Serialize the ``schedule`` JSON field the server expects.

    Args:
        crontab: A Quartz cron expression, e.g. ``0 0 3 * * ? *`` (03:00 daily).
        start_time/end_time: Active window in ``"yyyy-MM-dd HH:mm:ss"``.
        timezone_id: IANA timezone name used to interpret the cron expression.
    """
    return json.dumps(
        {
            "startTime": start_time,
            "endTime": end_time,
            "crontab": crontab,
            "timezoneId": timezone_id,
        }
    )


def create_schedule(
    client: DolphinSchedulerClient,
    project_code: int,
    workflow_definition_code: int,
    crontab: str,
    *,
    start_time: str = _DEFAULT_START,
    end_time: str = _DEFAULT_END,
    timezone_id: str = DEFAULT_TIMEZONE,
    failure_strategy: str = "CONTINUE",
    warning_type: str = "NONE",
    warning_group_id: int = 1,
    workflow_instance_priority: str = "MEDIUM",
    worker_group: str = "default",
    tenant_code: str = "default",
    environment_code: int = -1,
) -> dict[str, Any]:
    """Create a schedule for a workflow definition (created offline).

    Returns the created ``Schedule`` object; call :func:`set_schedule_state`
    with ``online=True`` to activate it.
    """
    schedule = build_schedule_payload(
        crontab,
        start_time=start_time,
        end_time=end_time,
        timezone_id=timezone_id,
    )
    return client.post(
        _schedule_base(project_code),
        data={
            "workflowDefinitionCode": workflow_definition_code,
            "schedule": schedule,
            "failureStrategy": failure_strategy,
            "warningType": warning_type,
            "warningGroupId": warning_group_id,
            "workflowInstancePriority": workflow_instance_priority,
            "workerGroup": worker_group,
            "tenantCode": tenant_code,
            "environmentCode": environment_code,
        },
    )


def set_schedule_state(
    client: DolphinSchedulerClient,
    project_code: int,
    schedule_id: int,
    online: bool,
) -> Any:
    """Bring a schedule online (active) or offline (paused)."""
    action = "online" if online else "offline"
    return client.post(f"{_schedule_base(project_code)}/{schedule_id}/{action}")


def list_schedules(
    client: DolphinSchedulerClient,
    project_code: int,
    *,
    page_no: int = 1,
    page_size: int = 50,
    workflow_definition_code: Optional[int] = None,
    search_val: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of schedules in a project."""
    return client.get(
        _schedule_base(project_code),
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "workflowDefinitionCode": workflow_definition_code,
            "searchVal": search_val,
        },
    )


def delete_schedule(
    client: DolphinSchedulerClient,
    project_code: int,
    schedule_id: int,
) -> Any:
    """Delete a schedule by id."""
    return client.delete(f"{_schedule_base(project_code)}/{schedule_id}")


def preview_schedule(
    client: DolphinSchedulerClient,
    project_code: int,
    crontab: str,
    *,
    start_time: str = _DEFAULT_START,
    end_time: str = _DEFAULT_END,
    timezone_id: str = DEFAULT_TIMEZONE,
) -> Any:
    """Ask the server for the next fire times of a cron expression.

    Useful as a dry run before creating a schedule — no state is changed.
    """
    schedule = build_schedule_payload(
        crontab,
        start_time=start_time,
        end_time=end_time,
        timezone_id=timezone_id,
    )
    return client.post(
        f"{_schedule_base(project_code)}/preview",
        data={"schedule": schedule},
    )
