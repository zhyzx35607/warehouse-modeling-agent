# cli-anything-dolphinscheduler

Structured CLI harness for Apache DolphinScheduler. It drives a running
DolphinScheduler API server through the real REST API and provides JSON output
for AI agents.

## Install

```bash
git clone git@github.com:ChenFatMan/cli-anything-dolphinscheduler.git
cd cli-anything-dolphinscheduler
chmod +x install.sh
./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

AI one-command install is documented in [INSTALL.md](../../INSTALL.md) and
[INSTALL.zh-CN.md](../../INSTALL.zh-CN.md).

## Configure

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_TOKEN=<access-token>
```

or:

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_USER=admin
export DS_PASSWORD=dolphinscheduler123
```

Verify:

```bash
cli-anything-dolphinscheduler login
cli-anything-dolphinscheduler --json project list
```

Connection settings resolve in this order:

1. CLI flags: `--url`, `--token`, `--user`, `--password`
2. Environment variables: `DS_URL`, `DS_TOKEN`, `DS_USER`, `DS_PASSWORD`
3. Config file: `~/.cli-anything-dolphinscheduler/config.json`
4. Built-in default: `http://localhost:12345/dolphinscheduler`

## Command Groups

| Group | Purpose |
|-------|---------|
| `project` | Create, list, get, select, update, and delete projects |
| `resource` | Manage Resource Center files and directories |
| `datasource` | Create, test, list, update, delete, and inspect datasources |
| `task` | Build `taskDefinitionJson` entries |
| `workflow` | Create shell workflows, list, release, delete definitions |
| `run` | Trigger, backfill, and control workflow execution |
| `instance` | Inspect workflow and task instances |
| `log` | Query and download task-instance logs |
| `schedule` | Create, list, preview, online, offline, and delete cron schedules |
| `token` | Create, generate, list, and delete API access tokens |
| `config` / `login` | Persist config and verify credentials |

All commands support `--json` from the root command.

## Project

```bash
project create <name> [--description TEXT]
project list [--search TEXT]
project get <name-or-code>
project use <name-or-code>
project current
project update <name-or-code> --name TEXT [--description TEXT]
project delete <name-or-code>
```

Example:

```bash
cli-anything-dolphinscheduler --json project create "Analytics"
cli-anything-dolphinscheduler project use "Analytics"
```

## Resource Center

Resource commands operate on DolphinScheduler's global Resource Center for the
authenticated user/tenant.

```bash
resource base-dir [--type FILE|ALL]
resource tree [--type FILE|ALL]
resource list --full-name TEXT [--search TEXT] [--page-no INT] [--page-size INT]
resource mkdir --name TEXT --current-dir TEXT
resource create-file --name FILE --current-dir TEXT (--content TEXT | --content-file PATH)
resource upload --path PATH --current-dir TEXT [--name FILE]
resource view <full-name> [--skip-line-num INT] [--limit INT]
resource update-content <full-name> (--content TEXT | --content-file PATH)
resource replace <full-name> --path PATH [--name FILE]
resource rename <full-name> <name>
resource download <full-name> --output PATH
resource delete <full-name> --yes
```

Examples:

```bash
cli-anything-dolphinscheduler --json resource base-dir

cli-anything-dolphinscheduler --json resource create-file \
  --name hello.py \
  --current-dir <directory-full-name> \
  --content "print('hello')"

cli-anything-dolphinscheduler --json resource upload \
  --path ./job.py \
  --current-dir <directory-full-name>

cli-anything-dolphinscheduler --json resource view <file-full-name>
cli-anything-dolphinscheduler --json resource download <file-full-name> --output ./job.py
```

## Datasource

Datasource commands use DolphinScheduler's native datasource parameter JSON.
This avoids hardcoding every datasource plugin schema in the CLI.

```bash
datasource list [--type MYSQL] [--search TEXT] [--page-no INT] [--page-size INT]
datasource get <datasource-id>
datasource create (--param-json JSON | --param-file PATH)
datasource update <datasource-id> (--param-json JSON | --param-file PATH)
datasource test-param (--param-json JSON | --param-file PATH)
datasource test <datasource-id>
datasource delete <datasource-id> --yes
datasource verify-name <name>
datasource kerberos-state
datasource databases <datasource-id>
datasource tables <datasource-id> <database>
datasource columns <datasource-id> <database> <table-name>
```

Example:

```bash
cli-anything-dolphinscheduler --json datasource test-param \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'

cli-anything-dolphinscheduler --json datasource create \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'

cli-anything-dolphinscheduler --json datasource tables <datasource-id> dolphinscheduler
```

## Task JSON

`workflow create-shell` is a shortcut. Use `task build-*` when an agent needs to
inspect or assemble `taskDefinitionJson` explicitly.

```bash
task build-shell --name TEXT --script TEXT [--code INT]
task build-python --name TEXT --script TEXT [--code INT]
task build-sql --name TEXT --sql TEXT --datasource INT [--code INT]
task build-http --name TEXT --url URL [--method GET|POST|PUT|DELETE|HEAD] [--code INT]
task build-generic --name TEXT --task-type TEXT --params-json JSON [--code INT]
```

Shared options include `--depends-on`, `--worker-group`, retry/timeout settings,
CPU quota, memory max, and `--project-code`.

Examples:

```bash
cli-anything-dolphinscheduler --json task build-python \
  --name py_task \
  --script "print('ok')" \
  --code 1001

cli-anything-dolphinscheduler --json task build-sql \
  --name query_task \
  --sql "select 1" \
  --datasource 10 \
  --code 1002

cli-anything-dolphinscheduler --json task build-generic \
  --name spark_job \
  --task-type SPARK \
  --params-json '{"mainClass":"org.example.Job"}' \
  --code 1003
```

`task build-generic` supports any DolphinScheduler task plugin when the caller
passes the exact `taskType` and `taskParams` JSON expected by the server/plugin.

## Workflow

```bash
workflow list [--project-code INT]
workflow create-shell --name TEXT --task "name:script" [--online]
workflow release <name-or-code> [--offline]
workflow delete <name-or-code>
```

Task spec format:

```text
name:script
name:script:dep1,dep2
```

Example:

```bash
cli-anything-dolphinscheduler workflow create-shell \
  --name "ETL Pipeline" \
  --task "extract:python extract.py" \
  --task "load:python load.py:extract" \
  --online
```

## Run

```bash
run start <name-or-code> [--dry-run]
run backfill <name-or-code> --start-date "yyyy-MM-dd HH:mm:ss" --end-date "yyyy-MM-dd HH:mm:ss"
run control <instance-id> STOP|PAUSE|REPEAT_RUNNING|RECOVER_SUSPENDED_PROCESS
```

Example:

```bash
cli-anything-dolphinscheduler --json run start "ETL Pipeline"
```

## Instance

```bash
instance list [--page-size INT]
instance get <instance-id>
instance delete <instance-id>
instance tasks <instance-id>
instance task-list [--workflow-instance-id INT] [--task-name TEXT] [--state TEXT]
instance force-task-success <task-instance-id>
instance stop-task <task-instance-id>
```

Failure triage:

```bash
cli-anything-dolphinscheduler --json instance task-list --state FAILURE
cli-anything-dolphinscheduler --json instance tasks <workflow-instance-id>
cli-anything-dolphinscheduler --json log detail <task-instance-id>
```

## Log

```bash
log detail <task-instance-id> [--skip-line-num INT] [--limit INT]
log download <task-instance-id> --output PATH
```

## Schedule

```bash
schedule create <name-or-code> --crontab "0 0 3 * * ? *" [--online]
schedule list
schedule preview --crontab "0 0 3 * * ? *"
schedule online <schedule-id>
schedule offline <schedule-id>
schedule delete <schedule-id> --yes
```

Crontab is a Quartz expression.

## Token

```bash
token create --user-id INT --expire-time "2030-01-01 00:00:00"
token generate --user-id INT --expire-time "2030-01-01 00:00:00"
token list
token delete <token-id> --yes
```

## REPL

Running with no subcommand starts an interactive session:

```bash
cli-anything-dolphinscheduler
dolphinscheduler ❯ project use Analytics
dolphinscheduler ❯ resource base-dir
dolphinscheduler ❯ workflow list
dolphinscheduler ❯ quit
```

## Agent Rules

- Prefer `--json` for every non-interactive call.
- Non-zero exit means failure; JSON errors are emitted on stderr.
- Select a project once with `project use <name-or-code>`, or pass
  `--project-code` per command.
- A workflow must be `ONLINE` before `run start`.
- Use `resource base-dir` before creating Resource Center files.
- Use `task build-* --json` to inspect task construction; pass `--code` for
  offline JSON, omit it to allocate a server task code.
- Use `instance task-list` and `instance tasks` before mutating failed task state.
- Use `log detail` before guessing why a task failed.
- Use `datasource test-param` before `datasource create` or `datasource update`.

## Current Boundaries

- High-level workflow creation is currently `workflow create-shell`.
- Non-shell task support exists through task JSON builders, not a complete typed
  workflow builder.
- Resource Center operations are supported, but task `resourceList` values must
  still match the real DolphinScheduler taskParams schema.
- Datasource creation accepts native JSON and relies on the real server/plugin
  for validation.
- The CLI does not fake server success; permissions, paths, and runtime behavior
  come from the real server.

## Development

```bash
python3 -m pytest cli_anything/dolphinscheduler/tests/test_core.py -v
CLI_ANYTHING_FORCE_INSTALLED=1 python3 -m pytest cli_anything/dolphinscheduler/tests/test_subprocess.py -v
```

## References

- [AGENT_USAGE.zh-CN.md](../../AGENT_USAGE.zh-CN.md) — Chinese Codex / AI agent runbook
- [COVERAGE_GAP.zh-CN.md](../../COVERAGE_GAP.zh-CN.md) — API coverage gap report
- [DOLPHINSCHEDULER.md](../../DOLPHINSCHEDULER.md) — REST API map
- [tests/TEST.md](tests/TEST.md) — test plan and results
- [Apache DolphinScheduler Docs](https://dolphinscheduler.apache.org/en-us/docs/latest)

## License

MIT
