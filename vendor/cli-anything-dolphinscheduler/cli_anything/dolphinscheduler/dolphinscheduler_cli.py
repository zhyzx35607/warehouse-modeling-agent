"""Command-line interface for Apache DolphinScheduler.

This CLI is a structured client to a *running* DolphinScheduler API server. It
does not reimplement scheduling — it drives the real REST API (the "backend
engine") for every operation, exactly as the web UI does.

Design:

* A root ``cli`` group resolves connection config and builds a shared
  :class:`Context` (client + session + output writer).
* Command groups mirror the server's domains: ``project``, ``workflow``,
  ``resource``, ``run``, ``instance``, ``schedule``, ``token``, plus ``config``
  and ``login``.
* Every command supports ``--json`` for machine-readable output.
* With no subcommand, the CLI drops into an interactive REPL.

Auth: pass an access token (``--token`` / ``DS_TOKEN``) or username+password
(``--user``/``--password`` or ``DS_USER``/``DS_PASSWORD``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import click

from .core import (
    datasources,
    executors,
    instances,
    logs,
    projects,
    resources,
    schedules,
    tasks,
    tokens,
    workflows,
)
from .core.client import DolphinSchedulerClient
from .core.config import ClientConfig, load_config, save_config
from .core.errors import DolphinSchedulerError
from .core.session import Session, load_session
from .utils.output import OutputWriter
from .utils.repl_skin import ReplSkin

__version__ = "1.0.0"


@dataclass
class Context:
    """Shared state threaded through every command via ``click`` context.

    Holds the resolved config, the persisted session, and the output writer.
    The API client is created lazily so commands that don't touch the network
    (e.g. ``config show``) work without a reachable server.
    """

    config: ClientConfig
    session: Session
    output: OutputWriter
    _client: Optional[DolphinSchedulerClient] = None

    @property
    def client(self) -> DolphinSchedulerClient:
        if self._client is None:
            self._client = DolphinSchedulerClient(self.config)
        return self._client

    def resolve_project(self, project_code: Optional[int]) -> int:
        """Pick an explicit ``--project-code`` or fall back to the session."""
        if project_code is not None:
            return project_code
        return self.session.require_project()


def _run(ctx: Context, func) -> None:
    """Execute a command body, translating typed errors into clean output.

    All domain calls funnel through here so a failed API call becomes a single
    formatted error (and a non-zero exit) rather than a Python traceback.
    """
    try:
        func()
    except DolphinSchedulerError as exc:
        ctx.output.error(exc.to_dict())
        sys.exit(1)
    except ValueError as exc:
        ctx.output.error({"error": "invalid_input", "message": str(exc)})
        sys.exit(1)
    except OSError as exc:
        ctx.output.error({"error": "filesystem_error", "message": str(exc)})
        sys.exit(1)


@click.group(invoke_without_command=True)
@click.option("--url", envvar="DS_URL", help="API base URL incl. /dolphinscheduler context path.")
@click.option("--token", envvar="DS_TOKEN", help="Access token sent in the 'token' header.")
@click.option("--user", envvar="DS_USER", help="Username for password login.")
@click.option("--password", envvar="DS_PASSWORD", help="Password for password login.")
@click.option("--timeout", type=float, envvar="DS_TIMEOUT", help="Per-request timeout (seconds).")
@click.option("--no-verify-tls", is_flag=True, default=False, help="Disable TLS certificate verification.")
@click.option("--project-code", type=int, default=None, help="Override the current project for this command.")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Emit machine-readable JSON output.")
@click.version_option(__version__, prog_name="cli-anything-dolphinscheduler")
@click.pass_context
def cli(
    ctx: click.Context,
    url: Optional[str],
    token: Optional[str],
    user: Optional[str],
    password: Optional[str],
    timeout: Optional[float],
    no_verify_tls: bool,
    project_code: Optional[int],
    json_mode: bool,
) -> None:
    """Structured CLI for a running Apache DolphinScheduler server."""
    config = load_config(
        url=url,
        token=token,
        user=user,
        password=password,
        timeout=timeout,
        verify_tls=False if no_verify_tls else None,
    )
    session = load_session()
    if project_code is not None:
        session.project_code = project_code
    skin = ReplSkin("dolphinscheduler", version=__version__)
    output = OutputWriter(json_mode, skin=skin)
    ctx.obj = Context(config=config, session=session, output=output)

    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.group()
def config() -> None:
    """Inspect and persist connection configuration."""


@config.command("show")
@click.pass_obj
def config_show(ctx: Context) -> None:
    """Show the resolved connection config (secrets masked)."""
    redacted = ctx.config.redacted()
    ctx.output.status_block(redacted, title="Connection", data=redacted)


@config.command("set")
@click.pass_obj
def config_set(ctx: Context) -> None:
    """Persist the current connection config to the config file."""
    def body() -> None:
        path = save_config(ctx.config)
        ctx.output.success(f"Saved config to {path}", data={"path": str(path)})

    _run(ctx, body)


@cli.command("login")
@click.pass_obj
def login_cmd(ctx: Context) -> None:
    """Verify credentials by logging in against the server."""
    def body() -> None:
        if not (ctx.config.user and ctx.config.password):
            raise DolphinSchedulerError(
                "login requires --user and --password (or DS_USER/DS_PASSWORD)."
            )
        payload = ctx.client.login(ctx.config.user, ctx.config.password)
        ctx.output.success("Login succeeded", data=payload)

    _run(ctx, body)


@cli.group()
def project() -> None:
    """Create, list, get, select, update, and delete projects."""


@project.command("create")
@click.argument("name")
@click.option("--description", default=None, help="Optional project description.")
@click.pass_obj
def project_create(ctx: Context, name: str, description: Optional[str]) -> None:
    """Create a new project."""
    def body() -> None:
        result = projects.create_project(ctx.client, name, description)
        ctx.output.success(f"Created project {name!r}", data=result)

    _run(ctx, body)


@project.command("list")
@click.option("--search", default=None, help="Filter by name substring.")
@click.pass_obj
def project_list(ctx: Context, search: Optional[str]) -> None:
    """List all projects."""
    def body() -> None:
        items = projects.list_all_projects(ctx.client)
        if search:
            items = [p for p in items if search.lower() in str(p.get("name", "")).lower()]
        rows = [[p.get("code"), p.get("name"), p.get("userName"), p.get("workflowDefinitionCount", 0)] for p in items]
        ctx.output.table(["code", "name", "owner", "workflows"], rows, data=items)

    _run(ctx, body)


@project.command("get")
@click.argument("name_or_code")
@click.pass_obj
def project_get(ctx: Context, name_or_code: str) -> None:
    """Show project detail by name or code."""
    def body() -> None:
        code = projects.resolve_project_code(ctx.client, name_or_code)
        detail = projects.get_project(ctx.client, code)
        if isinstance(detail, dict):
            ctx.output.status_block(detail, title="Project", data=detail)
        else:
            ctx.output.data(detail)

    _run(ctx, body)


@project.command("use")
@click.argument("name_or_code")
@click.pass_obj
def project_use(ctx: Context, name_or_code: str) -> None:
    """Select the current project (persisted across commands)."""
    def body() -> None:
        code = projects.resolve_project_code(ctx.client, name_or_code)
        detail = projects.get_project(ctx.client, code)
        name = (detail or {}).get("name") if isinstance(detail, dict) else None
        ctx.session.select_project(code, name)
        ctx.session.save()
        ctx.output.success(
            f"Using project {name or name_or_code} (code {code})",
            data={"project_code": code, "project_name": name},
        )

    _run(ctx, body)


@project.command("current")
@click.pass_obj
def project_current(ctx: Context) -> None:
    """Show the currently selected project."""
    data = {"project_code": ctx.session.project_code, "project_name": ctx.session.project_name}
    ctx.output.status_block(data, title="Current project", data=data)


@project.command("update")
@click.argument("name_or_code")
@click.option("--name", required=True, help="New project name.")
@click.option("--description", default=None, help="New project description.")
@click.pass_obj
def project_update(
    ctx: Context,
    name_or_code: str,
    name: str,
    description: Optional[str],
) -> None:
    """Rename a project or update its description."""
    def body() -> None:
        code = projects.resolve_project_code(ctx.client, name_or_code)
        result = projects.update_project(ctx.client, code, name, description)
        if ctx.session.project_code == code:
            ctx.session.select_project(code, name)
            ctx.session.save()
        ctx.output.success(
            f"Updated project {code}",
            data=result if result is not None else {"project_code": code},
        )

    _run(ctx, body)


@project.command("delete")
@click.argument("name_or_code")
@click.confirmation_option(prompt="Delete this project and all its workflows?")
@click.pass_obj
def project_delete(ctx: Context, name_or_code: str) -> None:
    """Delete a project by name or code."""
    def body() -> None:
        code = projects.resolve_project_code(ctx.client, name_or_code)
        projects.delete_project(ctx.client, code)
        if ctx.session.project_code == code:
            ctx.session.clear_project()
            ctx.session.save()
        ctx.output.success(f"Deleted project {name_or_code}", data={"project_code": code})

    _run(ctx, body)
# ── workflow group ───────────────────────────────────────────────────────────


@cli.group()
def workflow() -> None:
    """Create, list, release, and delete workflow definitions."""


@workflow.command("list")
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def workflow_list(ctx: Context, project_code: Optional[int]) -> None:
    """List workflow definitions in the current project."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        items = workflows.list_all_workflows(ctx.client, pcode)
        rows = []
        for entry in items:
            wf = entry.get("workflowDefinition", entry)
            rows.append([wf.get("code"), wf.get("name"), wf.get("releaseState"), wf.get("description", "")])
        ctx.output.table(["code", "name", "state", "description"], rows, data=items)

    _run(ctx, body)


@workflow.command("create-shell")
@click.option("--name", required=True, help="Workflow definition name.")
@click.option("--task", "tasks", multiple=True, required=True,
              help="Task spec 'name:script' or 'name:script:dep1,dep2'. Repeatable.")
@click.option("--project-code", type=int, default=None)
@click.option("--description", default="", help="Workflow description.")
@click.option("--online", is_flag=True, default=False, help="Release ONLINE after creation.")
@click.pass_obj
def workflow_create_shell(
    ctx: Context,
    name: str,
    tasks: tuple[str, ...],
    project_code: Optional[int],
    description: str,
    online: bool,
) -> None:
    """Create a workflow from one or more SHELL tasks."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        builder = workflows.DagBuilder(ctx.client, pcode)
        for spec in tasks:
            tname, script, deps = _parse_task_spec(spec)
            builder.add_shell(tname, script, depends_on=deps)
        task_json, relation_json = builder.build()
        result = workflows.create_workflow(
            ctx.client, pcode, name, task_json, relation_json, description=description
        )
        code = int(result["code"]) if isinstance(result, dict) and result.get("code") else None
        if online and code is not None:
            workflows.release_workflow(ctx.client, pcode, code, online=True)
        ctx.output.success(
            f"Created workflow {name!r}" + (" (ONLINE)" if online else ""),
            data=result,
        )

    _run(ctx, body)


@workflow.command("release")
@click.argument("name_or_code")
@click.option("--offline", is_flag=True, default=False, help="Take OFFLINE instead of ONLINE.")
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def workflow_release(ctx: Context, name_or_code: str, offline: bool, project_code: Optional[int]) -> None:
    """Bring a workflow ONLINE (runnable) or OFFLINE."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        code = workflows.resolve_workflow_code(ctx.client, pcode, name_or_code)
        workflows.release_workflow(ctx.client, pcode, code, online=not offline)
        state = "OFFLINE" if offline else "ONLINE"
        ctx.output.success(f"Workflow {code} is now {state}", data={"code": code, "state": state})

    _run(ctx, body)


@workflow.command("delete")
@click.argument("name_or_code")
@click.option("--project-code", type=int, default=None)
@click.confirmation_option(prompt="Delete this workflow definition?")
@click.pass_obj
def workflow_delete(ctx: Context, name_or_code: str, project_code: Optional[int]) -> None:
    """Delete a workflow definition."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        code = workflows.resolve_workflow_code(ctx.client, pcode, name_or_code)
        workflows.delete_workflow(ctx.client, pcode, code)
        ctx.output.success(f"Deleted workflow {code}", data={"code": code})

    _run(ctx, body)


# ── task group ───────────────────────────────────────────────────────────────


@cli.group()
def task() -> None:
    """Build task-definition JSON for workflow DAGs."""


def _task_common_options(func):
    """Apply shared task-definition options to a Click command."""
    options = [
        click.option("--code", type=int, default=None, help="Task code. If omitted, allocate one from the server."),
        click.option("--project-code", type=int, default=None, help="Project used for code allocation."),
        click.option("--description", default="", help="Task description."),
        click.option("--depends-on", multiple=True, help="Upstream task name. Repeatable."),
        click.option("--worker-group", default="default"),
        click.option("--task-priority", type=click.Choice(tasks.TASK_PRIORITIES), default="MEDIUM"),
        click.option("--fail-retry-times", type=int, default=0),
        click.option("--fail-retry-interval", type=int, default=1),
        click.option("--timeout", type=int, default=0),
        click.option("--delay-time", type=int, default=0),
        click.option("--cpu-quota", type=int, default=-1),
        click.option("--memory-max", type=int, default=-1),
    ]
    for option in reversed(options):
        func = option(func)
    return func


@task.command("build-generic")
@click.option("--name", required=True, help="Task name.")
@click.option("--task-type", required=True, help="DolphinScheduler taskType, e.g. SPARK, FLINK, DATAX.")
@click.option("--params-json", required=True, help="Task params JSON object.")
@_task_common_options
@click.pass_obj
def task_build_generic(ctx: Context, name: str, task_type: str, params_json: str, **kwargs: Any) -> None:
    """Build any task type from an explicit taskParams JSON object."""
    def command_body() -> None:
        task_code = _allocate_task_code(ctx, kwargs["project_code"], kwargs["code"])
        params = _parse_json_object(params_json, "params-json")
        task_def = tasks.TaskDefinition(
            name=name,
            task_type=task_type,
            task_params=params,
            code=task_code,
            **_task_common_kwargs(kwargs),
        )
        _emit_task_definition(ctx, task_def)

    _run(ctx, command_body)


@task.command("build-shell")
@click.option("--name", required=True, help="Task name.")
@click.option("--script", required=True, help="Shell script body.")
@_task_common_options
@click.pass_obj
def task_build_shell(ctx: Context, name: str, script: str, **kwargs: Any) -> None:
    """Build one SHELL taskDefinitionJson entry."""
    def command_body() -> None:
        task_code = _allocate_task_code(ctx, kwargs["project_code"], kwargs["code"])
        shell_task = tasks.ShellTask(
            name=name,
            script=script,
            code=task_code,
            **_task_common_kwargs(kwargs),
        )
        _emit_task_definition(ctx, shell_task)

    _run(ctx, command_body)


@task.command("build-python")
@click.option("--name", required=True, help="Task name.")
@click.option("--script", required=True, help="Python script body.")
@_task_common_options
@click.pass_obj
def task_build_python(ctx: Context, name: str, script: str, **kwargs: Any) -> None:
    """Build one PYTHON taskDefinitionJson entry."""
    def command_body() -> None:
        task_def = tasks.build_python_task(
            name=name,
            script=script,
            code=_allocate_task_code(ctx, kwargs["project_code"], kwargs["code"]),
            **_task_common_kwargs(kwargs),
        )
        _emit_task_definition(ctx, task_def)

    _run(ctx, command_body)


@task.command("build-sql")
@click.option("--name", required=True, help="Task name.")
@click.option("--sql", required=True, help="SQL text.")
@click.option("--datasource", type=int, required=True, help="DolphinScheduler datasource id.")
@click.option("--datasource-type", default="MYSQL", help="Datasource type, e.g. MYSQL, POSTGRESQL, HIVE.")
@click.option("--sql-type", type=click.Choice(tasks.SQL_TYPES), default="0", help="0=query, 1=non-query.")
@click.option("--display-rows", type=int, default=10)
@click.option("--pre-statement", "pre_statements", multiple=True)
@click.option("--post-statement", "post_statements", multiple=True)
@_task_common_options
@click.pass_obj
def task_build_sql(
    ctx: Context,
    name: str,
    sql: str,
    datasource: int,
    datasource_type: str,
    sql_type: str,
    display_rows: int,
    pre_statements: tuple[str, ...],
    post_statements: tuple[str, ...],
    **kwargs: Any,
) -> None:
    """Build one SQL taskDefinitionJson entry."""
    def command_body() -> None:
        task_def = tasks.build_sql_task(
            name=name,
            sql=sql,
            datasource=datasource,
            datasource_type=datasource_type,
            sql_type=sql_type,
            display_rows=display_rows,
            pre_statements=list(pre_statements),
            post_statements=list(post_statements),
            code=_allocate_task_code(ctx, kwargs["project_code"], kwargs["code"]),
            **_task_common_kwargs(kwargs),
        )
        _emit_task_definition(ctx, task_def)

    _run(ctx, command_body)


@task.command("build-http")
@click.option("--name", required=True, help="Task name.")
@click.option("--url", "request_url", required=True, help="Request URL.")
@click.option("--method", type=click.Choice(tasks.HTTP_METHODS), default="GET")
@click.option("--body", default="", help="Request body.")
@click.option("--param-json", "param_jsons", multiple=True, help="HTTP param JSON object. Repeatable.")
@click.option("--check-condition", type=click.Choice(tasks.HTTP_CHECK_CONDITIONS), default="STATUS_CODE_DEFAULT")
@click.option("--condition", default="", help="Custom check condition.")
@click.option("--connect-timeout", type=int, default=60000)
@click.option("--socket-timeout", type=int, default=60000)
@_task_common_options
@click.pass_obj
def task_build_http(
    ctx: Context,
    name: str,
    request_url: str,
    method: str,
    body: str,
    param_jsons: tuple[str, ...],
    check_condition: str,
    condition: str,
    connect_timeout: int,
    socket_timeout: int,
    **kwargs: Any,
) -> None:
    """Build one HTTP taskDefinitionJson entry."""
    def command_body() -> None:
        task_def = tasks.build_http_task(
            name=name,
            url=request_url,
            method=method,
            body=body,
            params=[_parse_json_object(param, "param-json") for param in param_jsons],
            check_condition=check_condition,
            condition=condition,
            connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            code=_allocate_task_code(ctx, kwargs["project_code"], kwargs["code"]),
            **_task_common_kwargs(kwargs),
        )
        _emit_task_definition(ctx, task_def)

    _run(ctx, command_body)


# ── resource group ──────────────────────────────────────────────────────────


@cli.group()
def resource() -> None:
    """Manage Resource Center files and directories."""


@resource.command("base-dir")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.pass_obj
def resource_base_dir(ctx: Context, resource_type: str) -> None:
    """Show the Resource Center base directory."""
    def body() -> None:
        full_name = resources.base_dir(ctx.client, resource_type)
        ctx.output.status_block(
            {"type": resource_type, "fullName": full_name},
            title="Resource base dir",
            data={"type": resource_type, "fullName": full_name},
        )

    _run(ctx, body)


@resource.command("tree")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.pass_obj
def resource_tree(ctx: Context, resource_type: str) -> None:
    """List the full Resource Center tree."""
    def body() -> None:
        items = resources.list_tree(ctx.client, resource_type)
        rows = [
            [
                item.get("name"),
                item.get("fullName"),
                item.get("isDirectory", item.get("isDirctory", item.get("dirctory"))),
                len(item.get("children") or []),
            ]
            for item in items
            if isinstance(item, dict)
        ]
        ctx.output.table(["name", "fullName", "dir", "children"], rows, data=items)

    _run(ctx, body)


@resource.command("list")
@click.option("--full-name", required=True, help="Directory fullName to list.")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.option("--search", "search_val", default=None, help="Filter by file name.")
@click.option("--page-no", type=int, default=1)
@click.option("--page-size", type=int, default=20)
@click.pass_obj
def resource_list(
    ctx: Context,
    full_name: str,
    resource_type: str,
    search_val: Optional[str],
    page_no: int,
    page_size: int,
) -> None:
    """List one Resource Center directory page."""
    def body() -> None:
        page = resources.list_items(
            ctx.client,
            full_name,
            resource_type=resource_type,
            search_val=search_val,
            page_no=page_no,
            page_size=page_size,
        )
        rows = [
            [
                item.get("fileName") or item.get("alias"),
                item.get("fullName"),
                item.get("isDirectory"),
                item.get("size"),
                item.get("updateTime"),
            ]
            for item in (page.get("totalList") or [])
        ]
        ctx.output.table(["name", "fullName", "dir", "size", "updated"], rows, data=page)

    _run(ctx, body)


@resource.command("mkdir")
@click.option("--name", required=True, help="Directory name to create.")
@click.option("--current-dir", required=True, help="Parent directory fullName.")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.pass_obj
def resource_mkdir(ctx: Context, name: str, current_dir: str, resource_type: str) -> None:
    """Create a Resource Center directory."""
    def body() -> None:
        result = resources.create_directory(ctx.client, name, current_dir, resource_type=resource_type)
        ctx.output.success(
            f"Created resource directory {name!r}",
            data={"name": name, "currentDir": current_dir, "type": resource_type, "result": result},
        )

    _run(ctx, body)


@resource.command("create-file")
@click.option("--name", required=True, help="File name with suffix, e.g. job.py.")
@click.option("--current-dir", required=True, help="Parent directory fullName.")
@click.option("--content", default=None, help="Inline file content.")
@click.option("--content-file", default=None, type=click.Path(exists=True, dir_okay=False), help="Read content from a local file.")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.pass_obj
def resource_create_file(
    ctx: Context,
    name: str,
    current_dir: str,
    content: Optional[str],
    content_file: Optional[str],
    resource_type: str,
) -> None:
    """Create a Resource Center file from text content."""
    def body() -> None:
        file_content = _read_content(content, content_file)
        result = resources.create_file_from_content(
            ctx.client,
            name,
            file_content,
            current_dir,
            resource_type=resource_type,
        )
        ctx.output.success(
            f"Created resource file {name!r}",
            data={"name": name, "currentDir": current_dir, "type": resource_type, "result": result},
        )

    _run(ctx, body)


@resource.command("upload")
@click.option("--path", "local_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Local file to upload.")
@click.option("--current-dir", required=True, help="Parent directory fullName.")
@click.option("--name", default=None, help="Resource file name. Defaults to local basename.")
@click.option("--type", "resource_type", type=click.Choice(resources.RESOURCE_TYPES), default="FILE")
@click.pass_obj
def resource_upload(
    ctx: Context,
    local_path: str,
    current_dir: str,
    name: Optional[str],
    resource_type: str,
) -> None:
    """Upload a local file into Resource Center."""
    def body() -> None:
        resource_name = name or Path(local_path).name
        result = resources.upload_file(
            ctx.client,
            local_path,
            current_dir,
            name=name,
            resource_type=resource_type,
        )
        ctx.output.success(
            f"Uploaded resource file {resource_name!r}",
            data={"name": resource_name, "currentDir": current_dir, "type": resource_type, "result": result},
        )

    _run(ctx, body)


@resource.command("view")
@click.argument("full_name")
@click.option("--skip-line-num", type=int, default=0)
@click.option("--limit", type=int, default=100, help="Line limit; use -1 for all.")
@click.pass_obj
def resource_view(ctx: Context, full_name: str, skip_line_num: int, limit: int) -> None:
    """View Resource Center text file content."""
    def body() -> None:
        result = resources.view_file(
            ctx.client,
            full_name,
            skip_line_num=skip_line_num,
            limit=limit,
        )
        if isinstance(result, dict):
            ctx.output.status_block(result, title="Resource content", data=result)
        else:
            ctx.output.data(result)

    _run(ctx, body)


@resource.command("update-content")
@click.argument("full_name")
@click.option("--content", default=None, help="Inline replacement content.")
@click.option("--content-file", default=None, type=click.Path(exists=True, dir_okay=False), help="Read replacement content from a local file.")
@click.pass_obj
def resource_update_content(
    ctx: Context,
    full_name: str,
    content: Optional[str],
    content_file: Optional[str],
) -> None:
    """Replace a Resource Center text file from inline or local content."""
    def body() -> None:
        file_content = _read_content(content, content_file)
        result = resources.update_file_content(ctx.client, full_name, file_content)
        ctx.output.success(
            f"Updated resource file {full_name!r}",
            data={"fullName": full_name, "result": result},
        )

    _run(ctx, body)


@resource.command("replace")
@click.argument("full_name")
@click.option("--path", "local_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Local file replacement.")
@click.option("--name", default=None, help="New resource file name. Defaults to local basename.")
@click.pass_obj
def resource_replace(ctx: Context, full_name: str, local_path: str, name: Optional[str]) -> None:
    """Replace a Resource Center file with a local file upload."""
    def body() -> None:
        resource_name = name or Path(local_path).name
        result = resources.update_file(ctx.client, full_name, local_path, name=name)
        ctx.output.success(
            f"Replaced resource file {full_name!r}",
            data={"fullName": full_name, "name": resource_name, "result": result},
        )

    _run(ctx, body)


@resource.command("rename")
@click.argument("full_name")
@click.argument("name")
@click.pass_obj
def resource_rename(ctx: Context, full_name: str, name: str) -> None:
    """Rename a Resource Center file or directory."""
    def body() -> None:
        result = resources.rename_resource(ctx.client, full_name, name)
        ctx.output.success(
            f"Renamed resource {full_name!r} to {name!r}",
            data={"fullName": full_name, "name": name, "result": result},
        )

    _run(ctx, body)


@resource.command("download")
@click.argument("full_name")
@click.option("--output", required=True, type=click.Path(dir_okay=False), help="Local output path.")
@click.pass_obj
def resource_download(ctx: Context, full_name: str, output: str) -> None:
    """Download a Resource Center file or directory archive."""
    def body() -> None:
        payload = resources.download_resource(ctx.client, full_name)
        output_path = Path(output)
        output_path.write_bytes(payload)
        ctx.output.success(
            f"Downloaded resource to {output_path}",
            data={"fullName": full_name, "output": str(output_path), "bytes": len(payload)},
        )

    _run(ctx, body)


@resource.command("delete")
@click.argument("full_name")
@click.confirmation_option(prompt="Delete this Resource Center entry?")
@click.pass_obj
def resource_delete(ctx: Context, full_name: str) -> None:
    """Delete a Resource Center file or directory."""
    def body() -> None:
        result = resources.delete_resource(ctx.client, full_name)
        ctx.output.success(
            f"Deleted resource {full_name!r}",
            data={"fullName": full_name, "result": result},
        )

    _run(ctx, body)


# ── datasource group ────────────────────────────────────────────────────────


@cli.group()
def datasource() -> None:
    """Manage datasource definitions and inspect database metadata."""


@datasource.command("list")
@click.option("--type", "datasource_type", default=None, help="DbType filter, e.g. MYSQL or POSTGRESQL.")
@click.option("--search", "search_val", default=None, help="Name search for paged listing.")
@click.option("--page-no", type=int, default=1)
@click.option("--page-size", type=int, default=20)
@click.pass_obj
def datasource_list(
    ctx: Context,
    datasource_type: Optional[str],
    search_val: Optional[str],
    page_no: int,
    page_size: int,
) -> None:
    """List visible datasources."""
    def body() -> None:
        if datasource_type:
            result = datasources.list_datasources_by_type(ctx.client, datasource_type)
            items = result if isinstance(result, list) else []
        else:
            result = datasources.list_datasources(
                ctx.client,
                search_val=search_val,
                page_no=page_no,
                page_size=page_size,
            )
            items = _page_items(result)
        rows = [
            [
                item.get("id"),
                item.get("name"),
                item.get("type"),
                item.get("userName") or item.get("user"),
                item.get("database"),
            ]
            for item in items
        ]
        ctx.output.table(["id", "name", "type", "user", "database"], rows, data=result)

    _run(ctx, body)


@datasource.command("get")
@click.argument("datasource_id", type=int)
@click.pass_obj
def datasource_get(ctx: Context, datasource_id: int) -> None:
    """Show one datasource definition."""
    def body() -> None:
        detail = datasources.get_datasource(ctx.client, datasource_id)
        if isinstance(detail, dict):
            ctx.output.status_block(detail, title="Datasource", data=detail)
        else:
            ctx.output.data(detail)

    _run(ctx, body)


@datasource.command("create")
@click.option("--param-json", default=None, help="Datasource parameter JSON object.")
@click.option("--param-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.pass_obj
def datasource_create(ctx: Context, param_json: Optional[str], param_file: Optional[str]) -> None:
    """Create a datasource from native DolphinScheduler datasource JSON."""
    def body() -> None:
        payload = _read_json_object_option(param_json, param_file, "datasource param")
        result = datasources.create_datasource(ctx.client, payload)
        ctx.output.success("Created datasource", data=result)

    _run(ctx, body)


@datasource.command("update")
@click.argument("datasource_id", type=int)
@click.option("--param-json", default=None, help="Datasource parameter JSON object.")
@click.option("--param-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.pass_obj
def datasource_update(
    ctx: Context,
    datasource_id: int,
    param_json: Optional[str],
    param_file: Optional[str],
) -> None:
    """Update a datasource from native DolphinScheduler datasource JSON."""
    def body() -> None:
        payload = _read_json_object_option(param_json, param_file, "datasource param")
        result = datasources.update_datasource(ctx.client, datasource_id, payload)
        ctx.output.success(f"Updated datasource {datasource_id}", data=result)

    _run(ctx, body)


@datasource.command("test-param")
@click.option("--param-json", default=None, help="Datasource parameter JSON object.")
@click.option("--param-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.pass_obj
def datasource_test_param(ctx: Context, param_json: Optional[str], param_file: Optional[str]) -> None:
    """Test datasource parameters before saving them."""
    def body() -> None:
        payload = _read_json_object_option(param_json, param_file, "datasource param")
        result = datasources.test_datasource_param(ctx.client, payload)
        ctx.output.success("Datasource parameter connection test succeeded", data=result)

    _run(ctx, body)


@datasource.command("test")
@click.argument("datasource_id", type=int)
@click.pass_obj
def datasource_test(ctx: Context, datasource_id: int) -> None:
    """Test an existing datasource connection."""
    def body() -> None:
        result = datasources.test_datasource(ctx.client, datasource_id)
        ctx.output.success(f"Datasource {datasource_id} connection test succeeded", data=result)

    _run(ctx, body)


@datasource.command("delete")
@click.argument("datasource_id", type=int)
@click.confirmation_option(prompt="Delete this datasource?")
@click.pass_obj
def datasource_delete(ctx: Context, datasource_id: int) -> None:
    """Delete a datasource."""
    def body() -> None:
        result = datasources.delete_datasource(ctx.client, datasource_id)
        ctx.output.success(
            f"Deleted datasource {datasource_id}",
            data={"datasource_id": datasource_id, "result": result},
        )

    _run(ctx, body)


@datasource.command("verify-name")
@click.argument("name")
@click.pass_obj
def datasource_verify_name(ctx: Context, name: str) -> None:
    """Verify whether a datasource name is available."""
    def body() -> None:
        result = datasources.verify_name(ctx.client, name)
        ctx.output.success(f"Datasource name {name!r} is available", data=result)

    _run(ctx, body)


@datasource.command("kerberos-state")
@click.pass_obj
def datasource_kerberos_state(ctx: Context) -> None:
    """Show datasource/resource Kerberos startup state."""
    def body() -> None:
        result = datasources.kerberos_startup_state(ctx.client)
        ctx.output.status_block({"kerberosStartupState": result}, title="Kerberos", data=result)

    _run(ctx, body)


@datasource.command("databases")
@click.argument("datasource_id", type=int)
@click.pass_obj
def datasource_databases(ctx: Context, datasource_id: int) -> None:
    """List databases visible through a datasource."""
    def body() -> None:
        result = datasources.list_databases(ctx.client, datasource_id)
        rows = [[_option_label(item), _option_value(item)] for item in _as_list(result)]
        ctx.output.table(["label", "value"], rows, data=result)

    _run(ctx, body)


@datasource.command("tables")
@click.argument("datasource_id", type=int)
@click.argument("database")
@click.pass_obj
def datasource_tables(ctx: Context, datasource_id: int, database: str) -> None:
    """List tables in one datasource database."""
    def body() -> None:
        result = datasources.list_tables(ctx.client, datasource_id, database)
        rows = [[_option_label(item), _option_value(item)] for item in _as_list(result)]
        ctx.output.table(["label", "value"], rows, data=result)

    _run(ctx, body)


@datasource.command("columns")
@click.argument("datasource_id", type=int)
@click.argument("database")
@click.argument("table_name")
@click.pass_obj
def datasource_columns(ctx: Context, datasource_id: int, database: str, table_name: str) -> None:
    """List columns for one datasource table."""
    def body() -> None:
        result = datasources.list_table_columns(ctx.client, datasource_id, database, table_name)
        rows = [[_option_label(item), _option_value(item)] for item in _as_list(result)]
        ctx.output.table(["label", "value"], rows, data=result)

    _run(ctx, body)


# ── run group (executors) ────────────────────────────────────────────────────


@cli.group()
def run() -> None:
    """Trigger and control workflow execution."""


@run.command("start")
@click.argument("name_or_code")
@click.option("--project-code", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_obj
def run_start(ctx: Context, name_or_code: str, project_code: Optional[int], dry_run: bool) -> None:
    """Trigger a single run of a workflow (must be ONLINE)."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        code = workflows.resolve_workflow_code(ctx.client, pcode, name_or_code)
        ids = executors.start_workflow(ctx.client, pcode, code, dry_run=dry_run)
        ctx.output.success(
            f"Triggered workflow {code}" + (" (dry run)" if dry_run else ""),
            data={"workflow_code": code, "instance_ids": ids},
        )

    _run(ctx, body)


@run.command("backfill")
@click.argument("name_or_code")
@click.option("--start-date", required=True, help="'yyyy-MM-dd HH:mm:ss' complement start.")
@click.option("--end-date", required=True, help="'yyyy-MM-dd HH:mm:ss' complement end.")
@click.option("--project-code", type=int, default=None)
@click.option("--run-mode", type=click.Choice(executors.RUN_MODES), default=None)
@click.option("--expected-parallelism-number", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_obj
def run_backfill(
    ctx: Context,
    name_or_code: str,
    start_date: str,
    end_date: str,
    project_code: Optional[int],
    run_mode: Optional[str],
    expected_parallelism_number: Optional[int],
    dry_run: bool,
) -> None:
    """Run complement-data/backfill for a workflow date range."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        code = workflows.resolve_workflow_code(ctx.client, pcode, name_or_code)
        ids = executors.backfill_workflow(
            ctx.client,
            pcode,
            code,
            start_date,
            end_date,
            run_mode=run_mode,
            expected_parallelism_number=expected_parallelism_number,
            dry_run=dry_run,
        )
        ctx.output.success(
            f"Backfill triggered for workflow {code}" + (" (dry run)" if dry_run else ""),
            data={
                "workflow_code": code,
                "start_date": start_date,
                "end_date": end_date,
                "instance_ids": ids,
            },
        )

    _run(ctx, body)


@run.command("control")
@click.argument("instance_id", type=int)
@click.argument("action", type=click.Choice(executors.EXECUTE_TYPES))
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def run_control(ctx: Context, instance_id: int, action: str, project_code: Optional[int]) -> None:
    """Control a running instance: STOP, PAUSE, REPEAT_RUNNING, etc."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        executors.control_instance(ctx.client, pcode, instance_id, action)
        ctx.output.success(f"Applied {action} to instance {instance_id}",
                           data={"instance_id": instance_id, "action": action})

    _run(ctx, body)


# ── instance group ───────────────────────────────────────────────────────────


@cli.group()
def instance() -> None:
    """Query workflow and task instances."""


@instance.command("list")
@click.option("--project-code", type=int, default=None)
@click.option("--page-size", type=int, default=20)
@click.pass_obj
def instance_list(ctx: Context, project_code: Optional[int], page_size: int) -> None:
    """List workflow instances in the current project."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        page = instances.list_workflow_instances(ctx.client, pcode, page_size=page_size)
        rows = [[i.get("id"), i.get("name"), i.get("state"), i.get("startTime"), i.get("endTime")]
                for i in (page.get("totalList") or [])]
        ctx.output.table(["id", "name", "state", "start", "end"], rows, data=page)

    _run(ctx, body)


@instance.command("get")
@click.argument("instance_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def instance_get(ctx: Context, instance_id: int, project_code: Optional[int]) -> None:
    """Show detail for one workflow instance."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        detail = instances.get_workflow_instance(ctx.client, pcode, instance_id)
        if isinstance(detail, dict):
            ctx.output.status_block(detail, title="Workflow instance", data=detail)
        else:
            ctx.output.data(detail)

    _run(ctx, body)


@instance.command("delete")
@click.argument("instance_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.confirmation_option(prompt="Delete this workflow instance?")
@click.pass_obj
def instance_delete(ctx: Context, instance_id: int, project_code: Optional[int]) -> None:
    """Delete a workflow instance."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        instances.delete_workflow_instance(ctx.client, pcode, instance_id)
        ctx.output.success(
            f"Deleted workflow instance {instance_id}",
            data={"instance_id": instance_id},
        )

    _run(ctx, body)


@instance.command("tasks")
@click.argument("instance_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def instance_tasks(ctx: Context, instance_id: int, project_code: Optional[int]) -> None:
    """List task instances that belong to one workflow instance."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        detail = instances.get_instance_tasks(ctx.client, pcode, instance_id)
        task_list = _task_list_from_payload(detail)
        rows = [
            [
                task.get("id"),
                task.get("name"),
                task.get("taskType"),
                task.get("state"),
                task.get("startTime"),
                task.get("endTime"),
                task.get("host"),
            ]
            for task in task_list
        ]
        ctx.output.table(
            ["id", "name", "type", "state", "start", "end", "host"],
            rows,
            data=detail,
        )

    _run(ctx, body)


@instance.command("task-list")
@click.option("--project-code", type=int, default=None)
@click.option("--workflow-instance-id", type=int, default=None)
@click.option("--workflow-instance-name", default=None)
@click.option("--workflow-definition-name", default=None)
@click.option("--task-name", default=None)
@click.option("--task-code", type=int, default=None)
@click.option("--executor-name", default=None)
@click.option("--state", "state_type", default=None, help="TaskExecutionStatus filter, e.g. SUCCESS.")
@click.option("--host", default=None)
@click.option("--search", "search_val", default=None)
@click.option("--start-date", default=None, help="'yyyy-MM-dd HH:mm:ss' lower bound.")
@click.option("--end-date", default=None, help="'yyyy-MM-dd HH:mm:ss' upper bound.")
@click.option("--task-execute-type", default=None, help="BATCH or STREAM.")
@click.option("--page-size", type=int, default=20)
@click.option("--page-no", type=int, default=1)
@click.pass_obj
def instance_task_list(
    ctx: Context,
    project_code: Optional[int],
    workflow_instance_id: Optional[int],
    workflow_instance_name: Optional[str],
    workflow_definition_name: Optional[str],
    task_name: Optional[str],
    task_code: Optional[int],
    executor_name: Optional[str],
    state_type: Optional[str],
    host: Optional[str],
    search_val: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    task_execute_type: Optional[str],
    page_size: int,
    page_no: int,
) -> None:
    """Search task instances across the current project."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        page = instances.list_task_instances(
            ctx.client,
            pcode,
            page_no=page_no,
            page_size=page_size,
            workflow_instance_id=workflow_instance_id,
            workflow_instance_name=workflow_instance_name,
            workflow_definition_name=workflow_definition_name,
            task_name=task_name,
            task_code=task_code,
            executor_name=executor_name,
            state_type=state_type,
            host=host,
            search_val=search_val,
            start_date=start_date,
            end_date=end_date,
            task_execute_type=task_execute_type,
        )
        rows = [
            [
                task.get("id"),
                task.get("name"),
                task.get("workflowInstanceId"),
                task.get("state"),
                task.get("executorName"),
                task.get("host"),
            ]
            for task in (page.get("totalList") or [])
        ]
        ctx.output.table(
            ["id", "name", "workflow", "state", "executor", "host"],
            rows,
            data=page,
        )

    _run(ctx, body)


@instance.command("force-task-success")
@click.argument("task_instance_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def instance_force_task_success(
    ctx: Context,
    task_instance_id: int,
    project_code: Optional[int],
) -> None:
    """Mark a failed task instance as successful."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        instances.force_success_task(ctx.client, pcode, task_instance_id)
        ctx.output.success(
            f"Marked task instance {task_instance_id} as success",
            data={"task_instance_id": task_instance_id},
        )

    _run(ctx, body)


@instance.command("stop-task")
@click.argument("task_instance_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def instance_stop_task(
    ctx: Context,
    task_instance_id: int,
    project_code: Optional[int],
) -> None:
    """Request stop for one running task instance."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        result = instances.stop_task(ctx.client, pcode, task_instance_id)
        ctx.output.success(
            f"Requested stop for task instance {task_instance_id}",
            data=result if result is not None else {"task_instance_id": task_instance_id},
        )

    _run(ctx, body)


# ── log group ────────────────────────────────────────────────────────────────


@cli.group()
def log() -> None:
    """Read and download task-instance logs."""


@log.command("detail")
@click.argument("task_instance_id", type=int)
@click.option("--skip-line-num", type=int, default=0)
@click.option("--limit", type=int, default=100)
@click.pass_obj
def log_detail(ctx: Context, task_instance_id: int, skip_line_num: int, limit: int) -> None:
    """Query task log content."""
    def body() -> None:
        result = logs.query_task_log(
            ctx.client,
            task_instance_id,
            skip_line_num=skip_line_num,
            limit=limit,
        )
        if ctx.output.json_mode:
            ctx.output.data(result)
            return
        if isinstance(result, dict) and "message" in result:
            click.echo(result["message"])
        elif isinstance(result, dict):
            ctx.output.status_block(result, title="Task log", data=result)
        else:
            click.echo(result)

    _run(ctx, body)


@log.command("download")
@click.argument("task_instance_id", type=int)
@click.option("--output", required=True, type=click.Path(dir_okay=False), help="Local output path.")
@click.pass_obj
def log_download(ctx: Context, task_instance_id: int, output: str) -> None:
    """Download the full task log to a local file."""
    def body() -> None:
        content = logs.download_task_log(ctx.client, task_instance_id)
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
        ctx.output.success(
            f"Downloaded task log to {output_path}",
            data={
                "task_instance_id": task_instance_id,
                "output": str(output_path),
                "bytes": len(content),
            },
        )

    _run(ctx, body)


# ── schedule group ───────────────────────────────────────────────────────────


@cli.group()
def schedule() -> None:
    """Manage cron schedules for workflows."""


@schedule.command("create")
@click.argument("name_or_code")
@click.option("--crontab", required=True, help="Quartz cron, e.g. '0 0 3 * * ? *'.")
@click.option("--project-code", type=int, default=None)
@click.option("--online", is_flag=True, default=False)
@click.pass_obj
def schedule_create(ctx: Context, name_or_code: str, crontab: str, project_code: Optional[int], online: bool) -> None:
    """Create a cron schedule for a workflow."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        code = workflows.resolve_workflow_code(ctx.client, pcode, name_or_code)
        result = schedules.create_schedule(ctx.client, pcode, code, crontab)
        sched_id = result.get("id") if isinstance(result, dict) else None
        if online and sched_id is not None:
            schedules.set_schedule_state(ctx.client, pcode, int(sched_id), online=True)
        ctx.output.success(f"Created schedule for workflow {code}" + (" (ONLINE)" if online else ""), data=result)

    _run(ctx, body)


@schedule.command("list")
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def schedule_list(ctx: Context, project_code: Optional[int]) -> None:
    """List schedules in the current project."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        page = schedules.list_schedules(ctx.client, pcode)
        rows = [[s.get("id"), s.get("workflowDefinitionCode"), s.get("crontab"), s.get("releaseState")]
                for s in (page.get("totalList") or [])]
        ctx.output.table(["id", "workflow", "crontab", "state"], rows, data=page)

    _run(ctx, body)


@schedule.command("preview")
@click.option("--crontab", required=True, help="Quartz cron, e.g. '0 0 3 * * ? *'.")
@click.option("--project-code", type=int, default=None)
@click.option("--start-time", default=schedules.DEFAULT_START_TIME, help="'yyyy-MM-dd HH:mm:ss' active-window start.")
@click.option("--end-time", default=schedules.DEFAULT_END_TIME, help="'yyyy-MM-dd HH:mm:ss' active-window end.")
@click.option("--timezone-id", default=schedules.DEFAULT_TIMEZONE)
@click.pass_obj
def schedule_preview(
    ctx: Context,
    crontab: str,
    project_code: Optional[int],
    start_time: str,
    end_time: str,
    timezone_id: str,
) -> None:
    """Preview next fire times without creating a schedule."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        result = schedules.preview_schedule(
            ctx.client,
            pcode,
            crontab,
            start_time=start_time,
            end_time=end_time,
            timezone_id=timezone_id,
        )
        rows = [[item] for item in _as_list(result)]
        ctx.output.table(["fire_time"], rows, data=result)

    _run(ctx, body)


@schedule.command("online")
@click.argument("schedule_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def schedule_online(ctx: Context, schedule_id: int, project_code: Optional[int]) -> None:
    """Bring a schedule ONLINE."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        result = schedules.set_schedule_state(ctx.client, pcode, schedule_id, online=True)
        ctx.output.success(f"Schedule {schedule_id} is ONLINE", data=result)

    _run(ctx, body)


@schedule.command("offline")
@click.argument("schedule_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.pass_obj
def schedule_offline(ctx: Context, schedule_id: int, project_code: Optional[int]) -> None:
    """Take a schedule OFFLINE."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        result = schedules.set_schedule_state(ctx.client, pcode, schedule_id, online=False)
        ctx.output.success(f"Schedule {schedule_id} is OFFLINE", data=result)

    _run(ctx, body)


@schedule.command("delete")
@click.argument("schedule_id", type=int)
@click.option("--project-code", type=int, default=None)
@click.confirmation_option(prompt="Delete this schedule?")
@click.pass_obj
def schedule_delete(ctx: Context, schedule_id: int, project_code: Optional[int]) -> None:
    """Delete a schedule."""
    def body() -> None:
        pcode = ctx.resolve_project(project_code)
        result = schedules.delete_schedule(ctx.client, pcode, schedule_id)
        ctx.output.success(
            f"Deleted schedule {schedule_id}",
            data={"schedule_id": schedule_id, "result": result},
        )

    _run(ctx, body)


# ── token group ──────────────────────────────────────────────────────────────


@cli.group()
def token() -> None:
    """Manage API access tokens."""


@token.command("create")
@click.option("--user-id", type=int, required=True)
@click.option("--expire-time", required=True, help="'yyyy-MM-dd HH:mm:ss' expiry.")
@click.option("--token-value", default=None, help="Explicit token string; omit to let the server generate one.")
@click.pass_obj
def token_create(ctx: Context, user_id: int, expire_time: str, token_value: Optional[str]) -> None:
    """Create an access token for a user."""
    def body() -> None:
        result = tokens.create_token(ctx.client, user_id, expire_time, token=token_value)
        ctx.output.success("Created access token", data=result)

    _run(ctx, body)


@token.command("generate")
@click.option("--user-id", type=int, required=True)
@click.option("--expire-time", required=True, help="'yyyy-MM-dd HH:mm:ss' expiry.")
@click.pass_obj
def token_generate(ctx: Context, user_id: int, expire_time: str) -> None:
    """Generate a token string without persisting it."""
    def body() -> None:
        result = tokens.generate_token_string(ctx.client, user_id, expire_time)
        ctx.output.success("Generated access token string", data={"token": result})

    _run(ctx, body)


@token.command("list")
@click.pass_obj
def token_list(ctx: Context) -> None:
    """List access tokens."""
    def body() -> None:
        page = tokens.list_tokens(ctx.client)
        rows = [[t.get("id"), t.get("userName"), t.get("expireTime")]
                for t in (page.get("totalList") or [])]
        ctx.output.table(["id", "user", "expires"], rows, data=page)

    _run(ctx, body)


@token.command("delete")
@click.argument("token_id", type=int)
@click.confirmation_option(prompt="Delete this access token?")
@click.pass_obj
def token_delete(ctx: Context, token_id: int) -> None:
    """Delete an access token."""
    def body() -> None:
        result = tokens.delete_token(ctx.client, token_id)
        ctx.output.success(
            f"Deleted access token {token_id}",
            data={"token_id": token_id, "result": result},
        )

    _run(ctx, body)


# ── REPL ─────────────────────────────────────────────────────────────────────


@cli.command()
@click.pass_obj
def repl(ctx: Context) -> None:
    """Start the interactive REPL (default when no subcommand is given)."""
    skin = ctx.output.skin
    skin.print_banner()
    if ctx.session.has_project:
        skin.info(f"Current project: {ctx.session.project_name or ctx.session.project_code}")
    else:
        skin.hint("No project selected. Use 'project use <name>' to pick one.")

    pt_session = skin.create_prompt_session()
    while True:
        try:
            line = skin.get_input(pt_session, project_name=ctx.session.project_name or "", context="")
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            break

        if not line:
            continue
        if line in ("quit", "exit", "q"):
            skin.print_goodbye()
            break
        if line in ("help", "?"):
            skin.help({
                "project create|list|get|use|update|delete": "Project operations",
                "task build-shell|build-python|build-sql|build-http|build-generic": "Build taskDefinitionJson",
                "resource tree|list|mkdir|create-file|upload": "Resource Center files",
                "resource view|update-content|replace|download|delete": "Resource file operations",
                "datasource list|get|create|update|test|delete": "Datasource definitions",
                "datasource databases|tables|columns": "Datasource metadata inspection",
                "workflow list|create-shell|release|delete": "Workflow definitions",
                "run start|backfill|control": "Trigger and control runs",
                "instance list|get|tasks|task-list": "Query workflow and task instances",
                "instance force-task-success|stop-task": "Task-instance controls",
                "log detail|download": "Task-instance logs",
                "schedule create|list|preview|online|offline|delete": "Cron schedules",
                "token create|generate|list|delete": "Access tokens",
                "config show|set, login": "Config and auth",
                "help, quit": "Show help, exit",
            })
            continue

        _dispatch_repl_line(line)


def _dispatch_repl_line(line: str) -> None:
    """Run a REPL line as if it were CLI args."""
    import shlex

    try:
        args = shlex.split(line)
    except ValueError as exc:
        click.echo(f"parse error: {exc}", err=True)
        return

    try:
        cli.main(args=args, standalone_mode=False)
    except SystemExit:
        pass
    except click.ClickException as exc:
        exc.show()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: {exc}", err=True)


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_task_spec(spec: str) -> tuple[str, str, list[str]]:
    """Parse 'name:script' or 'name:script:dep1,dep2'."""
    if ":" not in spec:
        raise ValueError(f"Task spec must be 'name:script': {spec!r}")
    name, rest = spec.split(":", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"Task spec has empty name: {spec!r}")

    deps: list[str] = []
    script = rest
    if ":" in rest:
        head, tail = rest.rsplit(":", 1)
        if tail and all(_is_identifier(d.strip()) for d in tail.split(",")):
            script, deps = head, [d.strip() for d in tail.split(",")]

    if not script.strip():
        raise ValueError(f"Task spec has empty script: {spec!r}")
    return name, script, deps


def _allocate_task_code(ctx: Context, project_code: Optional[int], code: Optional[int]) -> Optional[int]:
    """Use an explicit task code or ask the real API server for one."""
    if code is not None:
        return code
    pcode = ctx.resolve_project(project_code)
    return workflows.gen_task_codes(ctx.client, pcode, 1)[0]


def _task_common_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Normalize shared Click task options for task constructors."""
    return {
        "depends_on": list(kwargs["depends_on"]),
        "description": kwargs["description"],
        "worker_group": kwargs["worker_group"],
        "task_priority": kwargs["task_priority"],
        "fail_retry_times": kwargs["fail_retry_times"],
        "fail_retry_interval": kwargs["fail_retry_interval"],
        "timeout": kwargs["timeout"],
        "delay_time": kwargs["delay_time"],
        "cpu_quota": kwargs["cpu_quota"],
        "memory_max": kwargs["memory_max"],
    }


def _emit_task_definition(ctx: Context, task_def: Any) -> None:
    """Emit one constructed task definition in JSON or human-readable form."""
    definition = task_def.to_definition()
    data = {
        "taskDefinitionJson": tasks.dumps_task_definitions([task_def]),
        "task": definition,
        "depends_on": task_def.depends_on,
    }
    if ctx.output.json_mode:
        ctx.output.data(data)
        return
    ctx.output.status_block(
        {
            "code": definition.get("code"),
            "name": definition.get("name"),
            "taskType": definition.get("taskType"),
            "workerGroup": definition.get("workerGroup"),
            "dependsOn": ",".join(task_def.depends_on) or "-",
        },
        title=f"{definition.get('taskType')} task",
        data=data,
    )


def _parse_json_object(raw: str, label: str) -> dict[str, Any]:
    """Parse a CLI JSON string and require an object."""
    import json

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _read_json_object_option(
    json_value: Optional[str],
    json_file: Optional[str],
    label: str,
) -> dict[str, Any]:
    """Return exactly one JSON object source from CLI options."""
    if json_value is not None and json_file is not None:
        raise ValueError(f"pass either --param-json or --param-file for {label}, not both")
    if json_file is not None:
        return _parse_json_object(Path(json_file).read_text(encoding="utf-8"), label)
    if json_value is not None:
        return _parse_json_object(json_value, label)
    raise ValueError(f"pass --param-json or --param-file for {label}")


def _read_content(content: Optional[str], content_file: Optional[str]) -> str:
    """Return exactly one content source from CLI options."""
    if content is not None and content_file is not None:
        raise ValueError("pass either --content or --content-file, not both")
    if content_file is not None:
        return Path(content_file).read_text(encoding="utf-8")
    if content is not None:
        return content
    raise ValueError("pass --content or --content-file")


def _is_identifier(text: str) -> bool:
    return bool(text) and all(c.isalnum() or c in "-_" for c in text)


def _task_list_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize task-list API payloads to a list of task dictionaries."""
    if isinstance(payload, dict):
        items = payload.get("taskList") or payload.get("totalList") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def _page_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize DolphinScheduler PageInfo payloads to table rows."""
    if isinstance(payload, dict):
        for key in ("totalList", "records", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _as_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    return [] if payload is None else [payload]


def _option_label(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ("label", "name", "key", "value"):
            if key in item and item[key] is not None:
                return item[key]
    return item


def _option_value(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ("value", "name", "label", "key"):
            if key in item and item[key] is not None:
                return item[key]
    return item


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
