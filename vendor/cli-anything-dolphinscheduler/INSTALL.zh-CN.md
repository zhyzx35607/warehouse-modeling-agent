# cli-anything-dolphinscheduler 中文安装与使用说明

可以直接安装给 Codex。安装完成后，新的 Codex 会话会读取本仓库安装到
`~/.codex/skills/cli-anything-dolphinscheduler/SKILL.md` 的 skill，然后可以自己调用
`cli-anything-dolphinscheduler` 连接真实 DolphinScheduler API。

如果要把完整操作规程交给 AI 读，优先看 [Codex / AI Agent 使用手册](AGENT_USAGE.zh-CN.md)。

前提只有两个：

- DolphinScheduler API Server 可访问，例如 `http://localhost:12345/dolphinscheduler`
- Codex 拿得到凭据：`DS_TOKEN`，或 `DS_USER` / `DS_PASSWORD`

## 给 AI 自动安装的一条命令

把下面整条命令交给 Codex 执行：

```bash
REPO_URL="git@github.com:ChenFatMan/cli-anything-dolphinscheduler.git"; INSTALL_DIR="${HOME}/.local/share/cli-anything-dolphinscheduler"; mkdir -p "$(dirname "$INSTALL_DIR")"; if [ -d "$INSTALL_DIR/.git" ]; then git -C "$INSTALL_DIR" pull --ff-only; else git clone "$REPO_URL" "$INSTALL_DIR"; fi && cd "$INSTALL_DIR" && chmod +x install.sh && ./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

同一条命令保存在 `AI_INSTALL_COMMAND.txt`。

这条命令会安装：

- 本仓库 `.venv` 里的 Python 包
- 稳定命令入口 `~/.local/bin/cli-anything-dolphinscheduler`
- Codex skill：`~/.codex/skills/cli-anything-dolphinscheduler/SKILL.md`
- 通用 agent skill：`~/.agents/skills/cli-anything-dolphinscheduler/SKILL.md`
- 安装后验证和 subprocess 测试

如果已经在仓库根目录：

```bash
chmod +x install.sh
./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
```

## 安装给 Claude / OpenClaw / 其他 Agent

默认 `--install-skill` 会安装到：

```text
~/.codex/skills/cli-anything-dolphinscheduler/SKILL.md
~/.agents/skills/cli-anything-dolphinscheduler/SKILL.md
```

安装给 Claude Code 个人 skill：

```bash
./install.sh --dev --verify --install-bin --install-skill --skill-dir ~/.claude/skills
```

安装给当前项目的 Claude Code skill：

```bash
./install.sh --verify --install-skill --skill-dir .claude/skills
```

安装给 OpenClaw 全局 skill root：

```bash
./install.sh --dev --verify --install-bin --install-skill --skill-dir ~/.openclaw/skills
```

安装给 OpenClaw 当前 workspace 的 `skills/`：

```bash
./install.sh --verify --install-skill --skill-dir ./skills
```

如果本机有 OpenClaw CLI，也可以在 clone 后直接安装 skill：

```bash
openclaw skills install ./skills/cli-anything-dolphinscheduler \
  --as cli-anything-dolphinscheduler \
  --global
```

通用规则：只要某个 Agent 读取 `<skills-root>/<skill-name>/SKILL.md`，就用：

```bash
./install.sh --verify --install-skill --skill-dir <skills-root>
```

## 手动安装

```bash
cd cli-anything-dolphinscheduler
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -e '.[dev]'
cli-anything-dolphinscheduler --version
```

安装脚本常用参数：

| 参数 | 作用 |
|------|------|
| `--dev` | 安装测试依赖 |
| `--verify` | 安装后做 smoke check |
| `--force-installed-tests` | 强制用已安装命令跑 subprocess 测试 |
| `--install-skill` | 安装 AI skill 到 `~/.codex/skills` 和 `~/.agents/skills` |
| `--skill-dir DIR` | 指定额外 skill 根目录 |
| `--install-bin` | 安装 launcher 到 `~/.local/bin` |
| `--bin-dir DIR` | 指定 launcher 目录 |
| `--venv DIR` | 指定虚拟环境目录 |
| `--system` | 使用当前 Python 环境 |
| `--user` | 配合 `--system` 做用户级安装 |

## 配置 DolphinScheduler 连接

推荐使用 token：

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_TOKEN=<access-token>
```

也可以使用用户名和密码：

```bash
export DS_URL=http://localhost:12345/dolphinscheduler
export DS_USER=admin
export DS_PASSWORD=dolphinscheduler123
```

验证连接：

```bash
cli-anything-dolphinscheduler login
cli-anything-dolphinscheduler --json project list
```

持久化配置：

```bash
cli-anything-dolphinscheduler \
  --url http://localhost:12345/dolphinscheduler \
  --token <access-token> \
  config set
```

配置优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

## Codex 可以直接做什么

| 能力 | 命令 |
|------|------|
| 项目 | `project create/list/get/use/current/update/delete` |
| 资源中心 | `resource base-dir/tree/list/mkdir/create-file/upload/view/update-content/replace/rename/download/delete` |
| 数据源 | `datasource create/update/get/list/test/test-param/delete/verify-name/databases/tables/columns` |
| Task JSON | `task build-shell/build-python/build-sql/build-http/build-generic` |
| 工作流定义 | `workflow create-shell/list/release/delete` |
| 运行和补数 | `run start/backfill/control` |
| 实例排查 | `instance list/get/tasks/task-list/force-task-success/stop-task/delete` |
| 日志 | `log detail/download` |
| 定时 | `schedule create/list/preview/online/offline/delete` |
| Token | `token create/generate/list/delete` |

所有给 AI 解析的命令都建议加 `--json`。非零退出码表示失败，JSON 错误通常在 stderr。

## 资源中心操作

先拿 Resource Center 根目录：

```bash
cli-anything-dolphinscheduler --json resource base-dir
```

列目录：

```bash
cli-anything-dolphinscheduler --json resource list \
  --full-name <directory-full-name>
```

创建目录：

```bash
cli-anything-dolphinscheduler --json resource mkdir \
  --name scripts \
  --current-dir <parent-directory-full-name>
```

直接从文本创建文件：

```bash
cli-anything-dolphinscheduler --json resource create-file \
  --name hello.py \
  --current-dir <directory-full-name> \
  --content "print('hello')"
```

上传本地文件：

```bash
cli-anything-dolphinscheduler --json resource upload \
  --path ./job.py \
  --current-dir <directory-full-name>
```

查看、更新、下载、删除：

```bash
cli-anything-dolphinscheduler --json resource view <file-full-name>
cli-anything-dolphinscheduler --json resource update-content <file-full-name> --content-file ./job.py
cli-anything-dolphinscheduler --json resource download <file-full-name> --output ./job.py
cli-anything-dolphinscheduler --json resource delete <file-full-name> --yes
```

## Task 构造不是只有 SHELL

`workflow create-shell` 只是创建 shell DAG 的快捷入口。真正的 Task 构造在 `task` 分组：

```bash
cli-anything-dolphinscheduler task --help
```

已支持：

| 命令 | 任务类型 |
|------|----------|
| `task build-shell` | `SHELL` |
| `task build-python` | `PYTHON` |
| `task build-sql` | `SQL` |
| `task build-http` | `HTTP` |
| `task build-generic` | 任意 DolphinScheduler task plugin |

示例：

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

cli-anything-dolphinscheduler --json task build-http \
  --name health_check \
  --url "https://example.com/health" \
  --method GET \
  --code 1003

cli-anything-dolphinscheduler --json task build-generic \
  --name spark_job \
  --task-type SPARK \
  --params-json '{"mainClass":"org.example.Job"}' \
  --code 1004
```

`build-generic` 不猜参数。`--params-json` 必须是 DolphinScheduler server/plugin 真实期望的
`taskParams` JSON。

`--code` 规则：

- 传 `--code`：离线构造 JSON，不访问 server 分配 task code。
- 不传 `--code`：从当前项目或 `--project-code` 调用 API 分配真实 task code。

## 创建并运行一个最小工作流

```bash
cli-anything-dolphinscheduler --json project create "AgentProject"
cli-anything-dolphinscheduler project use "AgentProject"

cli-anything-dolphinscheduler workflow create-shell \
  --name "agent_smoke" \
  --task "hello:echo hello" \
  --online

cli-anything-dolphinscheduler --json run start "agent_smoke"
```

查看实例和失败任务：

```bash
cli-anything-dolphinscheduler --json instance list --page-size 10
cli-anything-dolphinscheduler --json instance tasks <workflow-instance-id>
cli-anything-dolphinscheduler --json instance task-list --state FAILURE
```

## 当前边界

- 高级工作流创建只有 `workflow create-shell` 快捷命令。
- 非 shell 工作流需要 AI 用 `task build-*` 生成 `taskDefinitionJson`，再按 DolphinScheduler API 形状组装工作流；后续可以继续封装 typed workflow builder。
- Resource Center 已支持文件/目录常用操作，但 task 的 `resourceList` 仍需要按 DolphinScheduler taskParams 真实结构传入。
- 真实运行、资源路径和权限都由 DolphinScheduler server 决定；CLI 不伪造成功。

## 测试

```bash
./install.sh --help
./install.sh --dev --verify --force-installed-tests --install-skill --install-bin
source .venv/bin/activate
python3 -m pytest cli_anything/dolphinscheduler/tests/test_core.py -v
CLI_ANYTHING_FORCE_INSTALLED=1 python3 -m pytest cli_anything/dolphinscheduler/tests/test_subprocess.py -v
```

真实 E2E 测试需要运行中的 DolphinScheduler API Server：

```bash
python3 -m pytest -m e2e cli_anything/dolphinscheduler/tests/test_full_e2e.py -v
```

## 卸载

```bash
python3 -m pip uninstall cli-anything-dolphinscheduler
rm -f ~/.local/bin/cli-anything-dolphinscheduler
rm -rf ~/.codex/skills/cli-anything-dolphinscheduler
rm -rf ~/.agents/skills/cli-anything-dolphinscheduler
```
