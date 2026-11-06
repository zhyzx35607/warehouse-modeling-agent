# DOLPHINSCHEDULER.md — Software Analysis & CLI SOP

Software-specific analysis and standard operating procedure for the
`cli-anything-dolphinscheduler` harness. This document records how the CLI maps
to the real Apache DolphinScheduler 3.4.2 REST API.

---

## 1. Backend Engine

Unlike file-rendering GUI apps (GIMP, Blender), DolphinScheduler is a
**distributed workflow-scheduling platform**. Its "backend engine" is a
**Spring Boot REST API server** — the same API the Vue web UI calls.

**The CLI is a structured client to this REST API. It does not reimplement
scheduling.** The API server is a hard dependency.

| Aspect | Value |
|--------|-------|
| Backend | Spring Boot REST API (`dolphinscheduler-api`) |
| Default port | `12345` |
| Context path | `/dolphinscheduler/` |
| Base URL | `http://<host>:12345/dolphinscheduler` |
| Runnable via | Docker Compose, or `StandaloneServer` (all-in-one JVM) |
| Default admin | `admin` / `dolphinscheduler123` |

## 2. Authentication Model

Verified from `LoginHandlerInterceptor.preHandle` (line 68: `request.getHeader("token")`).

Two mechanisms:

1. **Token auth (preferred for CLI)** — a DolphinScheduler access token sent in
   the literal `token` HTTP header. Stateless; ideal for automation.
2. **Session auth (fallback)** — `POST /login` with `userName`/`userPassword`
   form fields, which sets a `sessionId` cookie reused for later calls.

The CLI's `core/client.py` implements both: it sets the `token` header when a
token is configured, otherwise it logs in once and reuses the cookie jar.

## 3. Response Envelope

Every endpoint returns the `Result<T>` envelope (`api/utils/Result.java`):

```json
{ "code": 0, "msg": "success", "data": <T | null> }
```

- **Success is `code == 0`** — NOT the HTTP status. Many endpoints return
  HTTP 200/201 even for logical states.
- Paginated endpoints return `data` as a `PageInfo`:
  `{ "totalList": [...], "total", "totalPage", "currentPage", "pageSize" }`.

`core/client.py` unwraps this envelope and raises `APIError` on non-zero codes.

## 4. Data Model

| Entity | Identifier | Notes |
|--------|-----------|-------|
| Project | `long projectCode` | Top-level container (snowflake id) |
| Workflow definition | `long code` | A DAG of tasks under a project |
| Task definition | `long code` | A node in the DAG; codes from `gen-task-codes` |
| Workflow instance | `int id` | One execution of a definition |
| Task instance | `int id` | One execution of a node |
| Schedule | `int id` | Cron attached to a definition |
| Access token | `int id` | Stateless credential |
| Resource Center entry | `String fullName` | File or directory under the authenticated user/tenant storage root |

**Key insight:** A workflow definition is built from two parallel JSON documents
passed as form fields:
- `taskDefinitionJson` — list of task nodes (each with a unique `code`, `taskType`, `taskParams`)
- `taskRelationJson` — edges as `preTaskCode` → `postTaskCode` pairs (root task has `preTaskCode` 0)

The CLI's `DagBuilder` (`core/workflows.py`) generates both correctly, allocating
real task codes from the server's `gen-task-codes` endpoint.

Task construction lives in `core/tasks.py`. `TaskDefinition.to_definition()` is
the generic constructor for any `taskType` + `taskParams` pair, and
`ShellTask`/`build_python_task`/`build_sql_task`/`build_http_task` are typed
convenience constructors for high-frequency plugins. `DagBuilder` composes
those task objects with relations instead of owning task JSON shape.

## 5. Parameter Binding

Almost every endpoint binds `@RequestParam` (form-encoded / query-string fields),
even when a field value is itself JSON (`schedule`, `taskDefinitionJson`,
`startParams`). Exceptions using raw JSON bodies: DataSource create/update/connect,
Users `/batch/activate`, and file uploads (multipart).

The CLI sends form-encoded data by default (`core/client.py`), matching the
server's binding.

## 6. GUI Action → API Mapping

| GUI Action | REST Endpoint | CLI Command |
|------------|---------------|-------------|
| Create project | `POST /projects` | `project create` |
| List projects | `GET /projects/list` | `project list` |
| Get/update project | `GET /projects/{code}`, `PUT /projects/{code}` | `project get` / `project update` |
| Find resource base dir | `GET /resources/base-dir` | `resource base-dir` |
| List resource tree | `GET /resources/list` | `resource tree` |
| List resource directory | `GET /resources` | `resource list` |
| Create resource directory | `POST /resources/directory` | `resource mkdir` |
| Create text resource | `POST /resources/online-create` | `resource create-file` |
| Upload resource file | `POST /resources` | `resource upload` |
| View resource content | `GET /resources/view` | `resource view` |
| Update resource content | `PUT /resources/update-content` | `resource update-content` |
| Replace or rename resource | `PUT /resources` | `resource replace` / `resource rename` |
| Download resource | `GET /resources/download` | `resource download` |
| Delete resource | `DELETE /resources` | `resource delete` |
| Create/test datasource | `POST /datasources`, `POST /datasources/connect` | `datasource create` / `test-param` |
| Query datasource metadata | `GET /datasources/databases`, `/tables`, `/tableColumns` | `datasource databases` / `tables` / `columns` |
| Build task JSON | local constructors + optional `GET /projects/{code}/task-definition/gen-task-codes` | `task build-*` |
| Create workflow | `POST /projects/{code}/workflow-definition` | `workflow create-shell` |
| Publish workflow | `POST /projects/{code}/workflow-definition/{c}/release` | `workflow release` / `--online` |
| Run workflow | `POST /projects/{code}/executors/start-workflow-instance` | `run start` |
| Backfill workflow | `POST /projects/{code}/executors/start-workflow-instance` with `execType=COMPLEMENT_DATA` | `run backfill` |
| Stop/pause run | `POST /projects/{code}/executors/execute` | `run control` |
| View instances | `GET /projects/{code}/workflow-instances` | `instance list` / `get` |
| Delete workflow instance | `DELETE /projects/{code}/workflow-instances/{id}` | `instance delete` |
| View tasks in a run | `GET /projects/{code}/workflow-instances/{id}/tasks` | `instance tasks` |
| Search task instances | `GET /projects/{code}/task-instances` | `instance task-list` |
| Force failed task success | `POST /projects/{code}/task-instances/{id}/force-success` | `instance force-task-success` |
| Stop running task | `POST /projects/{code}/task-instances/{id}/stop` | `instance stop-task` |
| Read/download task log | `GET /log/detail`, `GET /log/download-log` | `log detail` / `download` |
| Create schedule | `POST /projects/{code}/schedules` | `schedule create` |
| Preview schedule | `POST /projects/{code}/schedules/preview` | `schedule preview` |
| Activate/deactivate schedule | `POST /projects/{code}/schedules/{id}/online`, `/offline` | `schedule online` / `offline` |
| Delete schedule | `DELETE /projects/{code}/schedules/{id}` | `schedule delete` |
| Create/generate API token | `POST /access-tokens`, `POST /access-tokens/generate` | `token create` / `token generate` |
| Delete API token | `DELETE /access-tokens/{id}` | `token delete` |

## 7. Key Endpoint Details

### Start a workflow
`POST /projects/{projectCode}/executors/start-workflow-instance` → `Result<List<Integer>>` (instance IDs)

Required form params: `workflowDefinitionCode`, `scheduleTime`, `failureStrategy`
(`CONTINUE`/`END`), `warningType` (`NONE`/`SUCCESS`/`FAILURE`/`ALL`),
`workflowInstancePriority` (`HIGHEST`..`LOWEST`). `execType` defaults to
`START_PROCESS`; `COMPLEMENT_DATA` triggers a backfill with a JSON `scheduleTime`.

### Construct a task definition
`taskDefinitionJson` is a JSON list of task nodes. The CLI now supports:

- `task build-shell` for SHELL `rawScript`
- `task build-python` for PYTHON `rawScript`
- `task build-sql` for SQL datasource-backed tasks
- `task build-http` for HTTP probe/request tasks
- `task build-generic` for every other plugin type by passing explicit
  `taskType` and `taskParams` JSON

All builders populate the common fields (`code`, `name`, `taskType`, `flag`,
retry/timeout/resource fields) and normalize common `taskParams` keys. When
`--code` is used, construction is fully local. Without `--code`, the CLI calls
`GET /projects/{projectCode}/task-definition/gen-task-codes` to allocate a real
server-side task code before rendering JSON.

### Datasources
`DataSourceController` is mounted at `/datasources`. The CLI intentionally
accepts native datasource JSON with `--param-json` / `--param-file` instead of
duplicating every datasource plugin's schema. Use `datasource test-param` before
`datasource create` or `datasource update`. Once saved, SQL tasks reference the
returned datasource `id`.

### Resource Center files
`ResourcesController` is mounted at `/resources` and is not project-scoped. The
CLI exposes the common file/directory operations agents need before building
tasks:

- `GET /resources/base-dir?type=FILE` discovers the writable storage root.
- `POST /resources/directory` creates directories under `currentDir`.
- `POST /resources/online-create` creates text files from `fileName`, `suffix`,
  `content`, and `currentDir`.
- `POST /resources` uploads multipart files.
- `GET /resources` pages a directory by `fullName`.
- `GET /resources/view` fetches text content.
- `PUT /resources/update-content` replaces text content.
- `PUT /resources` renames directories/files or replaces a file with multipart
  upload.
- `GET /resources/download` returns binary attachment content, so
  `core/client.py` has a dedicated `download()` method.
- `DELETE /resources?fullName=...` deletes a file or directory.

The stable identifier for Resource Center commands is `fullName`. Task
`resourceList` wiring still belongs inside the task plugin's expected
`taskParams` shape.

### Control an instance
`POST /projects/{projectCode}/executors/execute` with `workflowInstanceId` +
`executeType`: `STOP`, `PAUSE`, `REPEAT_RUNNING`, `RECOVER_SUSPENDED_PROCESS`,
`START_FAILURE_TASK_PROCESS`.

### Task-instance inspection and controls
`GET /projects/{projectCode}/workflow-instances/{id}/tasks` returns the tasks
inside one workflow instance, including task state and dependency context.
`GET /projects/{projectCode}/task-instances` is the paged cross-run task search
surface; the CLI exposes filters for workflow instance id/name, workflow
definition name, task name/code, executor, state, host, date range, and execute
type. `POST /projects/{projectCode}/task-instances/{id}/force-success` and
`POST /projects/{projectCode}/task-instances/{id}/stop` are surfaced for manual
recovery of failed or long-running task instances.

### Task logs
`LoggerController` is mounted at `/log`. `log detail` fetches paged text content
by `taskInstanceId`, `skipLineNum`, and `limit`. `log download` uses
`core/client.py` binary download handling and writes the server attachment to a
local file.

### Cron schedule
The `schedule` param is a JSON string:
`{"startTime","endTime","crontab","timezoneId"}`. Quartz cron (6/7 fields).
Lifecycle: preview → create (offline) → online/offline → delete.

## 8. End-to-End Run Order

The canonical "run from scratch" sequence the CLI automates:

```
create project → create workflow definition (with task JSON)
  → release ONLINE → executors/start-workflow-instance
  → poll workflow-instances for state
```

Or for scheduled runs: `create schedule → POST /{id}/online`.

## 9. Rendering / Output Verification

There is no "rendering gap" for this software — it is not a media/document tool.
"Correct output" means the **real server executed the workflow and the instance
reached `SUCCESS`**. The E2E test (`tests/test_full_e2e.py`) verifies this by:

1. Creating a real project + workflow via the API
2. Triggering a real run
3. Polling `workflow-instances/{id}` until terminal state
4. Asserting `state == "SUCCESS"` and all task instances succeeded

This is the API-server equivalent of "invoke the real software and verify the
output file" — we invoke the real scheduler and verify the run result.

## 10. Source References

Verified against DolphinScheduler 3.4.2 source:

- `dolphinscheduler-api/.../interceptor/LoginHandlerInterceptor.java` — `token` header auth
- `dolphinscheduler-api/.../utils/Result.java` — response envelope
- `dolphinscheduler-api/.../controller/ResourcesController.java` — Resource Center API
- `dolphinscheduler-api/.../controller/` — 33 endpoint-bearing REST controllers
- `dolphinscheduler-api/src/main/resources/application.yaml` — port 12345, context path
- `dolphinscheduler-api-test/.../workflow-json/test.json` — task/relation JSON shapes
