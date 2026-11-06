"""Project operations for DolphinScheduler.

A *project* is the top-level container in DolphinScheduler; workflows, schedules,
and instances all live under a project identified by its numeric ``code`` (a
snowflake long). These helpers wrap the ``projects`` controller and add the
name-to-code lookup that agents need, since most day-to-day references are by
human-readable name rather than code.
"""

from __future__ import annotations

from typing import Any, Optional

from .client import DolphinSchedulerClient
from .errors import NotFoundError

_PROJECTS = "/projects"


def create_project(
    client: DolphinSchedulerClient,
    name: str,
    description: Optional[str] = None,
) -> dict[str, Any]:
    """Create a project and return the created project payload.

    DolphinScheduler's create endpoint returns an empty ``data`` field, so we
    look the project back up by name to give the caller its assigned ``code``.
    """
    client.post(
        _PROJECTS,
        data={"projectName": name, "description": description},
    )
    return get_project_by_name(client, name)


def update_project(
    client: DolphinSchedulerClient,
    code: int,
    name: str,
    description: Optional[str] = None,
) -> Any:
    """Rename a project or change its description."""
    return client.put(
        f"{_PROJECTS}/{code}",
        data={"projectName": name, "description": description},
    )


def delete_project(client: DolphinSchedulerClient, code: int) -> Any:
    """Delete a project by code."""
    return client.delete(f"{_PROJECTS}/{code}")


def get_project(client: DolphinSchedulerClient, code: int) -> Any:
    """Fetch a single project by its numeric code."""
    return client.get(f"{_PROJECTS}/{code}")


def list_projects(
    client: DolphinSchedulerClient,
    page_no: int = 1,
    page_size: int = 50,
    search_val: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of projects (a ``PageInfo`` payload)."""
    return client.get(
        _PROJECTS,
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "searchVal": search_val,
        },
    )


def list_all_projects(client: DolphinSchedulerClient) -> list[dict[str, Any]]:
    """Return every project the user can see, unpaged."""
    data = client.get(f"{_PROJECTS}/list")
    return data or []


def get_project_by_name(client: DolphinSchedulerClient, name: str) -> dict[str, Any]:
    """Resolve a project by its unique name.

    Raises:
        NotFoundError: if no project with that exact name exists.
    """
    for project in list_all_projects(client):
        if project.get("name") == name:
            return project
    raise NotFoundError(f"No project named {name!r} was found")


def resolve_project_code(
    client: DolphinSchedulerClient,
    project: str,
) -> int:
    """Resolve a project reference (name or numeric code) to its code.

    Accepts either a numeric code (returned as-is) or a project name, which is
    looked up. This lets CLI users pass whichever they have on hand.
    """
    text = str(project).strip()
    if text.isdigit():
        return int(text)
    return int(get_project_by_name(client, text)["code"])
