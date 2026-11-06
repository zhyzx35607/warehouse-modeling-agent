"""Task log operations for DolphinScheduler."""

from __future__ import annotations

from typing import Any

from .client import DolphinSchedulerClient

_LOG = "/log"


def query_task_log(
    client: DolphinSchedulerClient,
    task_instance_id: int,
    *,
    skip_line_num: int = 0,
    limit: int = 100,
) -> Any:
    """Return a slice of task-instance log content."""
    return client.get(
        f"{_LOG}/detail",
        params={
            "taskInstanceId": task_instance_id,
            "skipLineNum": skip_line_num,
            "limit": limit,
        },
    )


def download_task_log(client: DolphinSchedulerClient, task_instance_id: int) -> bytes:
    """Download the full task-instance log file."""
    return client.download(f"{_LOG}/download-log", params={"taskInstanceId": task_instance_id})
