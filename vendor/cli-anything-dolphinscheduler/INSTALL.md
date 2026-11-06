# Install cli-anything-dolphinscheduler

Chinese guide: [INSTALL.zh-CN.md](INSTALL.zh-CN.md).

`cli-anything-dolphinscheduler` lets Codex and other agents operate a running
Apache DolphinScheduler API server. It calls the real REST API; it does not
reimplement scheduling.

## AI Auto-Install Command

Give this command to an AI agent:

```bash
REPO_URL="git@github.com:ChenFatMan/cli-anything-dolphinscheduler.git"; INSTALL_DIR="${HOME}/.local/share/cli-anything-dolphinscheduler"; mkdir -p "$(dirname "$INSTALL_DIR")"; if [ -d "$INSTALL_DIR/.git" ]; then git -C "$INSTALL_DIR" pull --ff-only; else git clone "$REPO_URL" "$INSTALL_DIR"; fi && cd "$INSTALL_DIR" && chmod +x install.sh && ./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

This installs:

- a local `.venv`
- the `cli-anything-dolphinscheduler` package
- an AI skill under `~/.codex/skills` and `~/.agents/skills`
- a stable launcher at `~/.local/bin/cli-anything-dolphinscheduler`

The same command is stored in `AI_INSTALL_COMMAND.txt`.

## Claude / OpenClaw / Other Agent Hosts

Default `--install-skill` installs to:

```text
~/.codex/skills/cli-anything-dolphinscheduler/SKILL.md
~/.agents/skills/cli-anything-dolphinscheduler/SKILL.md
```

Claude Code personal skill:

```bash
./install.sh --dev --verify --install-bin --install-skill --skill-dir ~/.claude/skills
```

Claude Code project-local skill:

```bash
./install.sh --verify --install-skill --skill-dir .claude/skills
```

OpenClaw global skill root:

```bash
./install.sh --dev --verify --install-bin --install-skill --skill-dir ~/.openclaw/skills
```

OpenClaw workspace-local `skills/` directory:

```bash
./install.sh --verify --install-skill --skill-dir ./skills
```

If the OpenClaw CLI is available after cloning this repo:

```bash
openclaw skills install ./skills/cli-anything-dolphinscheduler \
  --as cli-anything-dolphinscheduler \
  --global
```

Generic rule: if an agent reads `<skills-root>/<skill-name>/SKILL.md`, use:

```bash
./install.sh --verify --install-skill --skill-dir <skills-root>
```

## Manual Install

```bash
cd cli-anything-dolphinscheduler
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -e '.[dev]'
cli-anything-dolphinscheduler --version
```

Or from the repository root:

```bash
chmod +x install.sh
./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

Useful installer options:

```bash
./install.sh --help
./install.sh --verify
./install.sh --install-skill --install-bin --verify
./install.sh --venv /tmp/ds-cli-venv --verify
./install.sh --system --user --verify
```

## Configure DolphinScheduler

Token auth:

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_TOKEN=<access-token>
```

Username/password auth:

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

Persist config:

```bash
cli-anything-dolphinscheduler \
  --url http://localhost:12345/dolphinscheduler \
  --token <access-token> \
  config set
```

## Agent Quickstart

Use `--json` for machine-readable output.

```bash
cli-anything-dolphinscheduler --json project create "AgentProject"
cli-anything-dolphinscheduler project use "AgentProject"
cli-anything-dolphinscheduler --json workflow list
```

Resource Center:

```bash
cli-anything-dolphinscheduler --json resource base-dir
cli-anything-dolphinscheduler --json resource create-file \
  --name hello.py \
  --current-dir <directory-full-name> \
  --content "print('hello')"
cli-anything-dolphinscheduler --json resource upload \
  --path ./job.py \
  --current-dir <directory-full-name>
```

Task JSON:

```bash
cli-anything-dolphinscheduler --json task build-python \
  --name py_task \
  --script "print('ok')" \
  --code 1001

cli-anything-dolphinscheduler --json task build-generic \
  --name spark_job \
  --task-type SPARK \
  --params-json '{"mainClass":"org.example.Job"}' \
  --code 1002
```

Datasource:

```bash
cli-anything-dolphinscheduler --json datasource test-param \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'

cli-anything-dolphinscheduler --json datasource create \
  --param-json '{"type":"MYSQL","name":"agent_mysql","host":"localhost","port":3306,"userName":"root","password":"secret","database":"dolphinscheduler","other":{}}'
```

Run and inspect:

```bash
cli-anything-dolphinscheduler workflow create-shell \
  --name "agent_smoke" \
  --task "hello:echo hello" \
  --online
cli-anything-dolphinscheduler --json run start "agent_smoke"
cli-anything-dolphinscheduler --json instance task-list --state FAILURE
cli-anything-dolphinscheduler --json log detail <task-instance-id>
```

## Supported Areas

| Area | Commands |
|------|----------|
| Projects | `project create/list/get/use/current/update/delete` |
| Resource Center | `resource base-dir/tree/list/mkdir/create-file/upload/view/update-content/replace/rename/download/delete` |
| Datasources | `datasource create/update/get/list/test/test-param/delete/verify-name/databases/tables/columns` |
| Task JSON | `task build-shell/build-python/build-sql/build-http/build-generic` |
| Workflows | `workflow create-shell/list/release/delete` |
| Runs | `run start/backfill/control` |
| Instances | `instance list/get/tasks/task-list/force-task-success/stop-task/delete` |
| Logs | `log detail/download` |
| Schedules | `schedule create/list/preview/online/offline/delete` |
| Tokens | `token create/generate/list/delete` |

## Current Boundaries

- `workflow create-shell` is the only high-level workflow builder.
- Non-shell task types are supported through `task build-*` JSON builders; use
  `task build-generic` for plugin types and pass the exact server/plugin
  `taskParams`.
- Resource Center file/directory operations are supported. Task `resourceList`
  values still need to match DolphinScheduler's real taskParams shape.
- Datasource creation accepts native DolphinScheduler datasource JSON and lets
  the real server/plugin validate it.
- The CLI never fakes server success. Permissions, paths, and runtime behavior
  are decided by the real DolphinScheduler server.

## For AI Agents

After `--install-skill`, a new AI session can discover the skill at:

```text
~/.codex/skills/cli-anything-dolphinscheduler/SKILL.md
~/.agents/skills/cli-anything-dolphinscheduler/SKILL.md
```

If `cli-anything-dolphinscheduler` is not in `PATH`, call:

```bash
~/.local/bin/cli-anything-dolphinscheduler --json project list
```

## Uninstall

```bash
python3 -m pip uninstall cli-anything-dolphinscheduler
rm -f ~/.local/bin/cli-anything-dolphinscheduler
rm -rf ~/.codex/skills/cli-anything-dolphinscheduler
rm -rf ~/.agents/skills/cli-anything-dolphinscheduler
```
