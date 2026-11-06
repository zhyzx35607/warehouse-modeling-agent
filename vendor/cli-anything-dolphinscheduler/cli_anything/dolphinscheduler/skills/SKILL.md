---
name: cli-anything-dolphinscheduler
description: Use when the user wants an agent to install, configure, inspect, or operate Apache DolphinScheduler from Codex or a command line, including projects, Resource Center files, datasources, workflow definitions, task JSON, runs, instances, logs, schedules, tokens, and --json automation against a real DolphinScheduler API server.
---

# cli-anything-dolphinscheduler

Command-line control for a running Apache DolphinScheduler API server. The CLI
calls the real REST API; it does not simulate scheduling or fake success.

If this skill is being read from the repository checkout, read
`AGENT_USAGE.zh-CN.md` for the full Chinese agent runbook. This skill remains
self-contained when installed under `~/.codex/skills`.

## First Decision

| Situation | Action |
|-----------|--------|
| CLI missing | Run the auto-install command below |
| CLI installed but no server config | Configure `DS_URL` plus `DS_TOKEN` or `DS_USER` / `DS_PASSWORD` |
| Need machine parsing | Add root `--json` to every command |
| Need files/scripts in DS | Use `resource ...` commands |
| Need datasource setup | Use `datasource test-param`, then `datasource create` |
| Need non-shell task JSON | Use `task build-python/sql/http/generic` |
| Need to run a workflow | Ensure the workflow is `ONLINE`, then `run start` |
| Need failure triage | Use `instance task-list`, `instance tasks`, and `log detail` before mutation |

## Install for Codex

Run exactly once per machine:

```bash
REPO_URL="git@github.com:ChenFatMan/cli-anything-dolphinscheduler.git"; INSTALL_DIR="${HOME}/.local/share/cli-anything-dolphinscheduler"; mkdir -p "$(dirname "$INSTALL_DIR")"; if [ -d "$INSTALL_DIR/.git" ]; then git -C "$INSTALL_DIR" pull --ff-only; else git clone "$REPO_URL" "$INSTALL_DIR"; fi && cd "$INSTALL_DIR" && chmod +x install.sh && ./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

Other hosts from the repository root:

```bash
# Claude Code personal skill
./install.sh --verify --install-skill --skill-dir ~/.claude/skills

# OpenClaw global skill root
./install.sh --verify --install-skill --skill-dir ~/.openclaw/skills

# Any host that reads <skills-root>/<skill-name>/SKILL.md
./install.sh --verify --install-skill --skill-dir <skills-root>
```

Use `cli-anything-dolphinscheduler` if it is in `PATH`; otherwise use:

```bash
~/.local/bin/cli-anything-dolphinscheduler --version
```

## Connect

Preferred token auth:

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_TOKEN=<access-token>
```

Password auth:

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

## Command Groups

| Group | Covers |
|-------|--------|
| `project` | `create`, `list`, `get`, `use`, `current`, `update`, `delete` |
| `resource` | `base-dir`, `tree`, `list`, `mkdir`, `create-file`, `upload`, `view`, `update-content`, `replace`, `rename`, `download`, `delete` |
| `datasource` | `create`, `update`, `get`, `list`, `test`, `test-param`, `delete`, `verify-name`, `databases`, `tables`, `columns`, `kerberos-state` |
| `task` | `build-shell`, `build-python`, `build-sql`, `build-http`, `build-generic` |
| `workflow` | `create-shell`, `list`, `release`, `delete` |
| `run` | `start`, `backfill`, `control` |
| `instance` | `list`, `get`, `tasks`, `task-list`, `force-task-success`, `stop-task`, `delete` |
| `log` | `detail`, `download` |
| `schedule` | `create`, `list`, `preview`, `online`, `offline`, `delete` |
| `token` | `create`, `generate`, `list`, `delete` |

## Standard Agent Flow

```bash
cli-anything-dolphinscheduler --json project create "AgentProject"
cli-anything-dolphinscheduler project use "AgentProject"

cli-anything-dolphinscheduler workflow create-shell \
  --name "agent_smoke" \
  --task "hello:echo hello" \
  --online

cli-anything-dolphinscheduler --json run start "agent_smoke"
cli-anything-dolphinscheduler --json instance task-list --state FAILURE
cli-anything-dolphinscheduler --json log detail <task-instance-id>
```

## Resource Center

Always discover the base directory before creating files:

```bash
cli-anything-dolphinscheduler --json resource base-dir
```

Create files from inline or local content:

```bash
cli-anything-dolphinscheduler --json resource create-file \
  --name hello.py \
  --current-dir <directory-full-name> \
  --content "print('hello')"

cli-anything-dolphinscheduler --json resource upload \
  --path ./job.py \
  --current-dir <directory-full-name>
```

Inspect and mutate:

```bash
cli-anything-dolphinscheduler --json resource list --full-name <directory-full-name>
cli-anything-dolphinscheduler --json resource view <file-full-name>
cli-anything-dolphinscheduler --json resource update-content <file-full-name> --content-file ./job.py
cli-anything-dolphinscheduler --json resource download <file-full-name> --output ./job.py
cli-anything-dolphinscheduler --json resource delete <file-full-name> --yes
```

## Datasources

Datasource commands accept native DolphinScheduler datasource JSON. Test before
creating:

```bash
cli-anything-dolphinscheduler --json datasource test-param \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'

cli-anything-dolphinscheduler --json datasource create \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'

cli-anything-dolphinscheduler --json datasource test <datasource-id>
cli-anything-dolphinscheduler --json datasource databases <datasource-id>
cli-anything-dolphinscheduler --json datasource tables <datasource-id> <database>
cli-anything-dolphinscheduler --json datasource columns <datasource-id> <database> <table-name>
```

## Task JSON

`workflow create-shell` is only a shortcut. For other task plugins, build
`taskDefinitionJson` explicitly:

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

Rules:

- Pass `--code` for offline JSON construction.
- Omit `--code` to allocate a real task code from the selected project.
- Use `build-generic` for plugin types such as `SPARK`, `FLINK`, `DATAX`, `K8S`, and `SUB_PROCESS`.
- Do not invent `taskParams`; they must match the real DolphinScheduler plugin schema.

## Failure Handling

- Non-zero exit means failure.
- With `--json`, errors are written to stderr as `{"success": false, "error": "...", "message": "..."}`.
- `auth_error`: re-check token or username/password.
- `network_error`: verify `DS_URL` and API server reachability.
- `invalid_input`: fix CLI arguments before retrying.
- `api_error`: DolphinScheduler rejected the request; use the server message.

## Boundaries

- The CLI requires a reachable DolphinScheduler API server.
- High-level workflow creation currently covers shell DAGs through `workflow create-shell`.
- Non-shell task support exists through `task build-*` JSON builders.
- Resource Center file and directory operations are supported, but task `resourceList` wiring must match DolphinScheduler's real taskParams shape.
- Datasource creation uses native JSON and relies on the real server/plugin for validation.
- Do not report a successful DS operation unless the CLI exits 0.
