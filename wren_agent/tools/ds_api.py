"""Async adapter over cli-anything-dolphinscheduler core.

Replaces the in-store DS MCP wrapper (wren_agent/mcp_servers/ds_*.py, removed).
All DolphinScheduler calls go through ``cli_anything.dolphinscheduler.core`` —
the exact same code path as the ``cli-anything-dolphinscheduler`` CLI installed
in this venv — so anything the agent does can be reproduced by hand:

    cli-anything-dolphinscheduler --json project list

The core client is sync (requests); every call here is wrapped in
``asyncio.to_thread``. Errors surface as typed exceptions from
``cli_anything.dolphinscheduler.core.errors``:

- ``APIError``   — DS rejected the request; ``str(e)`` carries the real server msg
- ``AuthError``  — token invalid/expired
- ``NetworkError`` — DS unreachable
- ``NotFoundError`` — name lookup found nothing

Callers (deployment nodes) catch these and record actionable messages instead
of the old masked "NoneType" failures.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from cli_anything.dolphinscheduler.core import (
    datasources,
    executors,
    instances,
    logs as ds_logs,
    projects,
    schedules,
    workflows,
)
from cli_anything.dolphinscheduler.core.client import DolphinSchedulerClient
from cli_anything.dolphinscheduler.core.config import ClientConfig
from cli_anything.dolphinscheduler.core.errors import (  # noqa: F401  (re-exported)
    APIError,
    AuthError,
    DolphinSchedulerError,
    NetworkError,
    NotFoundError,
)
from cli_anything.dolphinscheduler.core.tasks import TaskDefinition, build_sql_task

from wren_agent.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "DSApi",
    "get_ds_api",
    "TaskDefinition",
    "build_sql_task",
    "APIError",
    "AuthError",
    "NetworkError",
    "NotFoundError",
    "DolphinSchedulerError",
]


def _build_client() -> DolphinSchedulerClient:
    cfg = ClientConfig(
        url=settings.ds.base_url,
        token=settings.ds.api_key,
        timeout=float(settings.ds.request_timeout),
    )
    return DolphinSchedulerClient(cfg)


class DSApi:
    """High-level async facade over the cli-anything DS core modules.

    One instance per deploy/execute node invocation. Not thread-safe for
    concurrent use of the same instance (requests.Session). Parallel ODS
    execution therefore builds one DSApi per in-flight workflow.
    """

    def __init__(self, client: Optional[DolphinSchedulerClient] = None):
        self._client = client or _build_client()

    async def _call(self, fn, /, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, self._client, *args, **kwargs)

    def close(self) -> None:
        self._client.close()

    # ── Projects ────────────────────────────────────────────────

    async def create_or_get_project(self, name: str, description: str = "") -> dict:
        """Create a DS project; if the name already exists, return it instead."""
        try:
            return await self._call(projects.create_project, name, description)
        except APIError as e:
            # Re-deploys reuse the same wren_{task_id[:8]} name — treat
            # "already exists" as success by resolving the existing project.
            logger.info("create_project '%s' rejected (%s); trying lookup", name, e)
            return await self._call(projects.get_project_by_name, name)

    # ── Datasources ─────────────────────────────────────────────

    async def find_datasource_id(self, name: str) -> Optional[int]:
        """Resolve a datasource id by exact name, or None."""
        page = await self._call(
            datasources.list_datasources, search_val=name, page_no=1, page_size=50
        )
        for row in (page or {}).get("totalList", []) or []:
            if row.get("name") == name:
                return int(row["id"])
        return None

    async def create_datasource(self, param: dict) -> Any:
        """Create a datasource from native DS datasource-param JSON.

        DS validates connectivity at creation; raises APIError when the
        connection test fails (e.g. placeholder credentials).
        """
        return await self._call(datasources.create_datasource, param)

    # ── Projects / workflows discovery (warehouse-mode DEPENDENT wiring) ──

    async def list_projects(self, search_val: str = "") -> list[dict]:
        """List all DS projects (unpaged best-effort, two pages of 50)."""
        from cli_anything.dolphinscheduler.core import projects
        out: list[dict] = []
        for page in (1, 2):
            page_data = await self._call(
                projects.list_projects, page_no=page, page_size=50, search_val=search_val or None
            )
            rows = (page_data or {}).get("totalList", []) or []
            out.extend(rows)
            if len(rows) < 50:
                break
        return out

    async def list_workflows(self, project_code: int) -> list[dict]:
        """List all workflow definitions in a project (unpaged)."""
        from cli_anything.dolphinscheduler.core import workflows
        return await self._call(workflows.list_all_workflows, project_code) or []

    async def get_workflow(self, project_code: int, code: int) -> dict:
        """Fetch full DAG detail (tasks) for one workflow definition."""
        from cli_anything.dolphinscheduler.core import workflows
        return await self._call(workflows.get_workflow, project_code, code)

    async def list_schedules(self, project_code: int, workflow_code: int) -> list[dict]:
        """List schedule definitions for one workflow."""
        from cli_anything.dolphinscheduler.core import schedules
        page = await self._call(
            schedules.list_schedules, project_code,
            page_no=1, page_size=50, workflow_definition_code=workflow_code,
        )
        return (page or {}).get("totalList", []) or []

    # ── Workflows ───────────────────────────────────────────────

    async def create_workflow(
        self,
        project_code: int,
        name: str,
        task_defs: list[TaskDefinition],
        relations: list[tuple[Optional[str], str]],
        *,
        description: str = "",
        global_params: Optional[list[dict]] = None,
    ) -> int:
        """Create a workflow from TaskDefinitions and named edges; return its code.

        Idempotent: if a workflow with the same name already exists in the
        project, it is REPLACED (offline → delete → recreate) so re-deploys
        pick up the new definition (task params / global params may have
        changed). Falls back to the existing code if replacement fails.

        ``relations`` is a list of (pre_task_name | None, post_task_name); None
        marks a DAG root. Real task codes are allocated server-side.

        ``global_params`` is a list of DS global-parameter dicts, e.g.
        ``[{"prop": "x", "value": "y", "direct": "IN", "type": "VARCHAR"}]`` —
        substituted into ``${x}`` placeholders at runtime (schedule and manual
        runs alike).
        """
        import json as _json

        codes = await self._call(
            workflows.gen_task_codes, project_code, len(task_defs)
        )
        name_to_code: dict[str, int] = {}
        for td, code in zip(task_defs, codes):
            td.code = code
            name_to_code[td.name] = code

        rel_json = []
        for pre, post in relations:
            rel_json.append({
                "name": "",
                "preTaskCode": name_to_code[pre] if pre else 0,
                "preTaskVersion": 0,
                "postTaskCode": name_to_code[post],
                "postTaskVersion": 0,
                "conditionType": "NONE",
                "conditionParams": {},
            })

        task_json = _json.dumps([td.to_definition() for td in task_defs])
        relation_json = _json.dumps(rel_json)
        gp_json = _json.dumps(global_params or [])

        try:
            result = await self._call(
                workflows.create_workflow,
                project_code,
                name,
                task_json,
                relation_json,
                description=description,
                global_params=gp_json,
            )
            return int((result or {}).get("code"))
        except APIError as e:
            # Re-deploys (or partial-success retries) hit name conflicts.
            # Replace the stale definition so the new one takes effect.
            if "already exists" not in str(e).lower():
                raise
            logger.info(
                "create_workflow '%s' exists (%s); replacing definition", name, e
            )
            old_code = await self._call(
                workflows.resolve_workflow_code, project_code, name
            )
            try:
                await self._delete_workflow_cascade(project_code, old_code)
                result = await self._call(
                    workflows.create_workflow,
                    project_code,
                    name,
                    task_json,
                    relation_json,
                    description=description,
                    global_params=gp_json,
                )
                return int((result or {}).get("code"))
            except DolphinSchedulerError as del_err:
                # Never worse than the old behavior: reuse the stale workflow.
                logger.warning(
                    "replace of workflow '%s' failed (%s); reusing existing code %s",
                    name, del_err, old_code,
                )
                return int(old_code)

    async def _delete_workflow_cascade(self, project_code: int, code: int) -> None:
        """Offline and delete a workflow, removing its schedule first."""
        sched_id = await self._find_schedule_id(project_code, code)
        if sched_id is not None:
            try:
                await self._call(
                    schedules.set_schedule_state, project_code, int(sched_id), False
                )
            except APIError as e:
                logger.info("schedule %s offline rejected (%s)", sched_id, e)
            await self._call(schedules.delete_schedule, project_code, int(sched_id))
        try:
            await self.release_workflow(project_code, code, online=False)
        except APIError as e:
            logger.info("workflow %s offline rejected (%s)", code, e)
        await self._call(workflows.delete_workflow, project_code, code)

    async def release_workflow(self, project_code: int, code: int, online: bool = True) -> Any:
        return await self._call(workflows.release_workflow, project_code, code, online)

    async def resolve_workflow_code(self, project_code: int, name: str) -> int:
        """Resolve a workflow name to its code (NotFoundError if absent)."""
        return await self._call(workflows.resolve_workflow_code, project_code, name)

    # ── Schedules ───────────────────────────────────────────────

    async def create_schedule_online(
        self, project_code: int, workflow_code: int, crontab: str
    ) -> Optional[int]:
        """Create a Quartz-cron schedule and bring it online; return its id.

        Idempotent: DS allows only one schedule per workflow, so re-deploys
        hit "schedule N already exist". Resolve the existing schedule instead
        of failing (mirrors create_workflow's already-exists handling).

        Schedules do nothing in DS until explicitly onlined.
        """
        try:
            sched = await self._call(
                schedules.create_schedule, project_code, workflow_code, crontab
            )
            sched_id = (sched or {}).get("id")
        except APIError as e:
            if "already exist" not in str(e).lower():
                raise
            logger.info(
                "create_schedule for workflow %s rejected (%s); trying lookup",
                workflow_code, e,
            )
            sched_id = await self._find_schedule_id(project_code, workflow_code)
        if sched_id is not None:
            try:
                await self._call(
                    schedules.set_schedule_state, project_code, int(sched_id), True
                )
            except APIError as e:
                # A resolved pre-existing schedule is typically online already;
                # DS rejects re-onlining it. Not a deploy failure.
                if "already" not in str(e).lower() and "online" not in str(e).lower():
                    raise
                logger.info("schedule %s already online (%s)", sched_id, e)
        return int(sched_id) if sched_id is not None else None

    async def _find_schedule_id(
        self, project_code: int, workflow_code: int
    ) -> Optional[int]:
        """Resolve the existing schedule id for a workflow, or None."""
        page = await self._call(
            schedules.list_schedules,
            project_code,
            workflow_definition_code=workflow_code,
        )
        for row in (page or {}).get("totalList", []) or []:
            row_wf = row.get("workflowDefinitionCode") or row.get("processDefinitionCode")
            if row_wf is None or int(row_wf) == int(workflow_code):
                return int(row["id"])
        return None

    # ── Execution ───────────────────────────────────────────────

    async def start_workflow(
        self,
        project_code: int,
        workflow_code: int,
        start_params: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """Trigger one run; return the created workflow-instance id (or None)."""
        ids = await self._call(
            executors.start_workflow, project_code, workflow_code,
            start_params=start_params,
        )
        return int(ids[0]) if ids else None

    async def get_workflow_instance(self, project_code: int, instance_id: int) -> dict:
        return await self._call(
            instances.get_workflow_instance, project_code, instance_id
        )

    async def get_instance_tasks(self, project_code: int, instance_id: int) -> list[dict]:
        data = await self._call(
            instances.get_instance_tasks, project_code, instance_id
        )
        # /tasks returns {"taskList": [...]} on some DS versions, a bare list on others
        if isinstance(data, dict):
            return data.get("taskList", []) or []
        return data or []

    async def query_task_log(
        self, task_instance_id: int, *, skip_line_num: int = 0, limit: int = 200
    ) -> str:
        data = await self._call(
            ds_logs.query_task_log, task_instance_id,
            skip_line_num=skip_line_num, limit=limit,
        )
        if isinstance(data, dict):
            return str(data.get("message", "") or data.get("log", "") or data)
        return str(data or "")


def get_ds_api() -> DSApi:
    """Build a fresh DSApi from wren settings (one per node invocation)."""
    return DSApi()
