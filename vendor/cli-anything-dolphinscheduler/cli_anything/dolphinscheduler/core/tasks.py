"""Task-definition construction for DolphinScheduler workflows.

DolphinScheduler workflow creation requires a ``taskDefinitionJson`` document:
a JSON list of task nodes, each with a server-unique ``code``, a ``taskType``,
common scheduling fields, and task-type-specific ``taskParams``.

This module is the single source of truth for building those task-definition
objects. Higher-level workflow code can focus on DAG relations instead of
duplicating task JSON shape details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

TASK_PRIORITIES = ("HIGHEST", "HIGH", "MEDIUM", "LOW", "LOWEST")
RUN_FLAGS = ("YES", "NO")
TIMEOUT_FLAGS = ("OPEN", "CLOSE")
TASK_EXECUTE_TYPES = ("BATCH", "STREAM")
SQL_TYPES = ("0", "1")
HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")
HTTP_CHECK_CONDITIONS = (
    "STATUS_CODE_DEFAULT",
    "STATUS_CODE_CUSTOM",
    "BODY_CONTAINS",
    "BODY_NOT_CONTAINS",
)


@dataclass
class TaskDefinition:
    """A generic task node ready to serialize into ``taskDefinitionJson``.

    This is the escape hatch for every DolphinScheduler task plugin. Pass the
    exact ``taskType`` and ``taskParams`` required by the real server/plugin.
    Convenience wrappers below build common task types on top of this shape.
    """

    name: str
    task_type: str
    task_params: dict[str, Any]
    code: Optional[int] = None
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    flag: str = "YES"
    task_priority: str = "MEDIUM"
    worker_group: str = "default"
    environment_code: int = -1
    fail_retry_times: int = 0
    fail_retry_interval: int = 1
    timeout_flag: str = "CLOSE"
    timeout_notify_strategy: str = ""
    timeout: int = 0
    delay_time: int = 0
    cpu_quota: int = -1
    memory_max: int = -1
    task_execute_type: str = "BATCH"

    def __post_init__(self) -> None:
        _validate_common(self)
        if not isinstance(self.task_params, dict):
            raise ValueError("task_params must be a JSON object")

    def to_definition(self) -> dict[str, Any]:
        """Render this task as one ``taskDefinitionJson`` entry."""
        return _common_definition(self, _with_common_params(self.task_params))


def build_dependent_task(
    *,
    name: str,
    depend_items: list[dict],
    relation: str = "AND",
    code: Optional[int] = None,
    depends_on: Optional[list[str]] = None,
    **common: Any,
) -> TaskDefinition:
    """Build a DEPENDENT task that waits for other workflows/tasks to finish.

    Each ``depend_items`` entry follows the DolphinScheduler DEPENDENT plugin
    shape: {"depTaskType": "DEP_WORKFLOW"|"DEP_TASK", "projectCode": "...",
    "definitionCode": "...", "depCycle": "DAY", "dateValue": "today"}.

    Used by warehouse mode so a new DWS/ADS DAG waits on the already-deployed
    upstream DWD/DWM workflow instances (see wayfinder T-11).
    """
    _require_text("name", name)
    if not depend_items:
        raise ValueError("depend_items must not be empty")
    return TaskDefinition(
        name=name,
        task_type="DEPENDENT",
        task_params={
            "localParams": [],
            "resourceList": [],
            # DS DependentParameters requires the dependTaskList wrapper —
            # a bare {relation, dependItemList} is rejected with
            # "workflow node <name> parameter invalid" (probe-verified).
            "dependence": {
                "relation": relation,
                "dependTaskList": [{
                    "relation": relation,
                    "dependItemList": list(depend_items),
                }],
            },
        },
        code=code,
        depends_on=list(depends_on or []),
        **common,
    )


def build_python_task(
    *,
    name: str,
    script: str,
    code: Optional[int] = None,
    depends_on: Optional[list[str]] = None,
    **common: Any,
) -> TaskDefinition:
    """Build a PYTHON task using the same rawScript params as the UI."""
    _require_text("script", script)
    return TaskDefinition(
        name=name,
        task_type="PYTHON",
        task_params=_script_params(script),
        code=code,
        depends_on=list(depends_on or []),
        **common,
    )


def build_sql_task(
    *,
    name: str,
    sql: str,
    datasource: int,
    code: Optional[int] = None,
    depends_on: Optional[list[str]] = None,
    datasource_type: str = "MYSQL",
    sql_type: str = "0",
    display_rows: int = 10,
    sql_source: str = "SCRIPT",
    sql_resource: str = "",
    pre_statements: Optional[list[str]] = None,
    post_statements: Optional[list[str]] = None,
    **common: Any,
) -> TaskDefinition:
    """Build a SQL task definition.

    ``sql_type`` follows DolphinScheduler's UI model: ``0`` for query and ``1``
    for non-query execution.
    """
    _require_text("sql", sql)
    _validate_choice("sql_type", sql_type, SQL_TYPES)
    if int(datasource) <= 0:
        raise ValueError("datasource must be a positive integer")
    return TaskDefinition(
        name=name,
        task_type="SQL",
        task_params=_with_common_params(
            {
                "type": datasource_type,
                "datasource": int(datasource),
                "sql": sql,
                "sqlType": sql_type,
                "displayRows": display_rows,
                "sqlSource": sql_source,
                "sqlResource": sql_resource,
                "preStatements": list(pre_statements or []),
                "postStatements": list(post_statements or []),
            }
        ),
        code=code,
        depends_on=list(depends_on or []),
        **common,
    )


def build_http_task(
    *,
    name: str,
    url: str,
    code: Optional[int] = None,
    depends_on: Optional[list[str]] = None,
    method: str = "GET",
    body: str = "",
    params: Optional[list[dict[str, Any]]] = None,
    check_condition: str = "STATUS_CODE_DEFAULT",
    condition: str = "",
    connect_timeout: int = 60000,
    socket_timeout: int = 60000,
    **common: Any,
) -> TaskDefinition:
    """Build an HTTP task definition."""
    _require_text("url", url)
    method = method.upper()
    _validate_choice("method", method, HTTP_METHODS)
    _validate_choice("check_condition", check_condition, HTTP_CHECK_CONDITIONS)
    _validate_non_negative("connect_timeout", connect_timeout)
    _validate_non_negative("socket_timeout", socket_timeout)
    return TaskDefinition(
        name=name,
        task_type="HTTP",
        task_params=_with_common_params(
            {
                "url": url,
                "httpMethod": method,
                "httpBody": body,
                "httpParams": list(params or []),
                "httpCheckCondition": check_condition,
                "condition": condition,
                "connectTimeout": connect_timeout,
                "socketTimeout": socket_timeout,
            }
        ),
        code=code,
        depends_on=list(depends_on or []),
        **common,
    )


@dataclass
class ShellTask:
    """A SHELL task node ready to serialize into ``taskDefinitionJson``."""

    name: str
    script: str
    code: Optional[int] = None
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    flag: str = "YES"
    task_priority: str = "MEDIUM"
    worker_group: str = "default"
    environment_code: int = -1
    fail_retry_times: int = 0
    fail_retry_interval: int = 1
    timeout_flag: str = "CLOSE"
    timeout_notify_strategy: str = ""
    timeout: int = 0
    delay_time: int = 0
    cpu_quota: int = -1
    memory_max: int = -1
    task_execute_type: str = "BATCH"
    local_params: list[dict[str, Any]] = field(default_factory=list)
    resource_list: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_text("script", self.script)
        _validate_common(self)

    def to_definition(self) -> dict[str, Any]:
        """Render this task as one ``taskDefinitionJson`` entry."""
        return _common_definition(self, self._task_params(), task_type="SHELL")

    def _task_params(self) -> dict[str, Any]:
        params = _script_params(self.script)
        params["localParams"] = list(self.local_params)
        params["resourceList"] = list(self.resource_list)
        return params


def dumps_task_definitions(tasks: list[Any]) -> str:
    """Serialize task objects into the JSON string accepted by the API."""
    if not tasks:
        raise ValueError("At least one task is required")
    return json.dumps([task.to_definition() for task in tasks])


def _script_params(script: str) -> dict[str, Any]:
    return _with_common_params({"rawScript": script})


def _with_common_params(params: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(params)
    normalized.setdefault("localParams", [])
    normalized.setdefault("resourceList", [])
    normalized.setdefault("dependence", {})
    normalized.setdefault("conditionResult", {"successNode": [], "failedNode": []})
    normalized.setdefault("waitStartTimeout", {})
    normalized.setdefault("switchResult", {})
    return normalized


def _common_definition(source: Any, task_params: dict[str, Any], task_type: Optional[str] = None) -> dict[str, Any]:
    return {
        "code": source.code,
        "name": source.name,
        "version": 1,
        "description": source.description,
        "delayTime": source.delay_time,
        "taskType": task_type or source.task_type,
        "taskParams": task_params,
        "flag": source.flag,
        "taskPriority": source.task_priority,
        "workerGroup": source.worker_group,
        "environmentCode": source.environment_code,
        "failRetryTimes": source.fail_retry_times,
        "failRetryInterval": source.fail_retry_interval,
        "timeoutFlag": source.timeout_flag,
        "timeoutNotifyStrategy": source.timeout_notify_strategy,
        "timeout": source.timeout,
        "cpuQuota": source.cpu_quota,
        "memoryMax": source.memory_max,
        "taskExecuteType": source.task_execute_type,
    }


def _validate_common(task: Any) -> None:
    _require_text("name", task.name)
    if hasattr(task, "task_type"):
        _require_text("task_type", task.task_type)
    _validate_choice("flag", task.flag, RUN_FLAGS)
    _validate_choice("task_priority", task.task_priority, TASK_PRIORITIES)
    _validate_choice("timeout_flag", task.timeout_flag, TIMEOUT_FLAGS)
    _validate_choice("task_execute_type", task.task_execute_type, TASK_EXECUTE_TYPES)
    _validate_non_negative("fail_retry_times", task.fail_retry_times)
    _validate_non_negative("timeout", task.timeout)
    _validate_non_negative("delay_time", task.delay_time)
    if task.fail_retry_interval < 1:
        raise ValueError("fail_retry_interval must be >= 1")
    if task.code is not None and int(task.code) <= 0:
        raise ValueError("code must be a positive integer")
    task.depends_on = [_clean_dependency(dep) for dep in task.depends_on]


def _require_text(field_name: str, value: str) -> None:
    if not str(value).strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_choice(field_name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(
            f"Invalid {field_name}={value!r}; expected one of {', '.join(allowed)}"
        )


def _validate_non_negative(field_name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0")


def _clean_dependency(value: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("depends_on entries must not be empty")
    return text
