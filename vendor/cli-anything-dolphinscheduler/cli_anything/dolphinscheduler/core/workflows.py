"""Workflow definition operations for DolphinScheduler.

A *workflow definition* is a DAG of tasks. DolphinScheduler represents it with
two parallel JSON documents passed as form fields:

* ``taskDefinitionJson`` — the list of task nodes (each with a unique ``code``,
  a ``taskType`` such as ``SHELL``, and ``taskParams``).
* ``taskRelationJson`` — the edges, expressed as ``preTaskCode`` →
  ``postTaskCode`` pairs. A root task has ``preTaskCode`` of ``0``.

Hand-building these documents is the part most likely to trip up an agent, so
this module provides a :class:`DagBuilder` that produces well-formed task and
relation lists (allocating real server-side task codes) alongside the raw CRUD
wrappers.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .client import DolphinSchedulerClient
from .errors import NotFoundError
from .tasks import ShellTask, dumps_task_definitions


def _wf_base(project_code: int) -> str:
    return f"/projects/{project_code}/workflow-definition"


# ── DAG construction ────────────────────────────────────────────────────────


class DagBuilder:
    """Assemble a task/relation JSON pair for a workflow definition.

    Usage:
        builder = DagBuilder(client, project_code)
        builder.add_shell("extract", "python extract.py")
        builder.add_shell("load", "python load.py", depends_on=["extract"])
        task_json, relation_json = builder.build()

    The builder allocates real task codes from the server's ``gen-task-codes``
    endpoint so the resulting definition is accepted as-is.
    """

    def __init__(self, client: DolphinSchedulerClient, project_code: int):
        self._client = client
        self._project_code = project_code
        self._tasks: list[ShellTask] = []

    def add_shell(
        self,
        name: str,
        script: str,
        *,
        depends_on: Optional[list[str]] = None,
        description: str = "",
        worker_group: str = "default",
        fail_retry_times: int = 0,
        fail_retry_interval: int = 1,
    ) -> "DagBuilder":
        """Add a SHELL task. Returns self so calls can be chained."""
        if any(task.name == name for task in self._tasks):
            raise ValueError(f"Duplicate task name in DAG: {name!r}")
        self._tasks.append(
            ShellTask(
                name=name,
                script=script,
                depends_on=list(depends_on or []),
                description=description,
                worker_group=worker_group,
                fail_retry_times=fail_retry_times,
                fail_retry_interval=fail_retry_interval,
            )
        )
        return self

    def build(self) -> tuple[str, str]:
        """Allocate codes, validate edges, and return (taskJson, relationJson)."""
        if not self._tasks:
            raise ValueError("A workflow must contain at least one task")

        codes = gen_task_codes(self._client, self._project_code, len(self._tasks))
        name_to_code: dict[str, int] = {}
        for task, code in zip(self._tasks, codes):
            task.code = code
            name_to_code[task.name] = code

        definitions = json.loads(dumps_task_definitions(self._tasks))
        relations = self._build_relations(name_to_code)

        return json.dumps(definitions), json.dumps(relations)

    def _build_relations(self, name_to_code: dict[str, int]) -> list[dict[str, Any]]:
        """Translate ``depends_on`` names into pre/post code edges."""
        relations: list[dict[str, Any]] = []
        for task in self._tasks:
            if not task.depends_on:
                # Root task: preTaskCode 0 marks a DAG entry point.
                relations.append(_relation(0, name_to_code[task.name]))
                continue
            for parent in task.depends_on:
                if parent not in name_to_code:
                    raise ValueError(
                        f"Task {task.name!r} depends on unknown task {parent!r}"
                    )
                relations.append(
                    _relation(name_to_code[parent], name_to_code[task.name])
                )
        return relations


def _relation(pre_code: int, post_code: int) -> dict[str, Any]:
    return {
        "name": "",
        "preTaskCode": pre_code,
        "preTaskVersion": 0,
        "postTaskCode": post_code,
        "postTaskVersion": 0,
        "conditionType": "NONE",
        "conditionParams": {},
    }


def gen_task_codes(
    client: DolphinSchedulerClient,
    project_code: int,
    count: int,
) -> list[int]:
    """Ask the server for ``count`` fresh, collision-free task codes.

    The endpoint lives on the task-definition controller and requires a project
    code in its path, even though the codes it returns are globally unique
    snowflake IDs.
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    codes = client.get(
        f"/projects/{project_code}/task-definition/gen-task-codes",
        params={"genNum": count},
    )
    return [int(code) for code in codes]


# ── CRUD operations ─────────────────────────────────────────────────────────


def create_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    name: str,
    task_definition_json: str,
    task_relation_json: str,
    *,
    description: str = "",
    global_params: str = "[]",
    locations: Optional[str] = None,
    timeout: int = 0,
    execution_type: str = "PARALLEL",
) -> dict[str, Any]:
    """Create a workflow definition from raw task/relation JSON documents."""
    return client.post(
        _wf_base(project_code),
        data={
            "name": name,
            "description": description,
            "globalParams": global_params,
            "locations": locations,
            "timeout": timeout,
            "taskDefinitionJson": task_definition_json,
            "taskRelationJson": task_relation_json,
            "executionType": execution_type,
        },
    )


def release_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    code: int,
    online: bool,
) -> Any:
    """Bring a workflow ONLINE (runnable) or take it OFFLINE."""
    state = "ONLINE" if online else "OFFLINE"
    return client.post(
        f"{_wf_base(project_code)}/{code}/release",
        data={"releaseState": state},
    )


def get_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    code: int,
) -> Any:
    """Fetch full DAG detail for one workflow definition."""
    return client.get(f"{_wf_base(project_code)}/{code}")


def list_workflows(
    client: DolphinSchedulerClient,
    project_code: int,
    page_no: int = 1,
    page_size: int = 50,
    search_val: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of workflow definitions in a project."""
    return client.get(
        _wf_base(project_code),
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "searchVal": search_val,
        },
    )


def list_all_workflows(
    client: DolphinSchedulerClient,
    project_code: int,
) -> list[dict[str, Any]]:
    """Return every workflow definition in a project, unpaged."""
    data = client.get(f"{_wf_base(project_code)}/all")
    return data or []


def delete_workflow(
    client: DolphinSchedulerClient,
    project_code: int,
    code: int,
) -> Any:
    """Delete a workflow definition by code."""
    return client.delete(f"{_wf_base(project_code)}/{code}")


def get_workflow_by_name(
    client: DolphinSchedulerClient,
    project_code: int,
    name: str,
) -> dict[str, Any]:
    """Resolve a workflow definition by name within a project.

    The ``/all`` payload nests the definition under a ``workflowDefinition``
    key; we normalize to that inner object.

    Raises:
        NotFoundError: when no workflow with that name exists.
    """
    for entry in list_all_workflows(client, project_code):
        definition = entry.get("workflowDefinition", entry)
        if definition.get("name") == name:
            return definition
    raise NotFoundError(
        f"No workflow named {name!r} in project {project_code}"
    )


def resolve_workflow_code(
    client: DolphinSchedulerClient,
    project_code: int,
    workflow: str,
) -> int:
    """Resolve a workflow reference (name or numeric code) to its code."""
    text = str(workflow).strip()
    if text.isdigit():
        return int(text)
    return int(get_workflow_by_name(client, project_code, text)["code"])
