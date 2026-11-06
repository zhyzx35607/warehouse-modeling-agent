# DolphinScheduler API 覆盖缺口

本文件给维护者和 AI agent 读，用来判断 `cli-anything-dolphinscheduler` 还缺哪些 DolphinScheduler REST API 封装。

扫描来源：

- DolphinScheduler Controller：`dolphinscheduler-api/src/main/java/org/apache/dolphinscheduler/api/controller`
- 当前扫描结果：33 个 Controller，约 243 个映射 endpoint
- 当前 CLI：`project`、`resource`、`datasource`、`task`、`workflow`、`run`、`instance`、`log`、`schedule`、`token`、`config`、`login`

## 已覆盖或基本覆盖

| API 域 | Controller | CLI 状态 |
|--------|------------|----------|
| 登录 | `LoginController` | `login` 支持用户名密码验证；SSO/OIDC/cookie 清理未封装 |
| 项目基础 CRUD | `ProjectController` | `project create/list/get/use/current/update/delete` 已有；授权列表未暴露为命令 |
| 资源中心 | `ResourcesController` | `resource base-dir/tree/list/mkdir/create-file/upload/view/update-content/replace/rename/download/delete` 覆盖主要文件和目录操作 |
| 数据源 | `DataSourceController` | `datasource create/update/get/list/test/test-param/delete/verify-name/databases/tables/columns/kerberos-state` 已新增 |
| Task JSON 构造 | `TaskDefinitionController` + task plugin schema | `task build-shell/python/sql/http/generic` 可构造 taskDefinitionJson；不等于完整 task-definition CRUD |
| 工作流定义基础操作 | `WorkflowDefinitionController` | `workflow create-shell/list/release/delete` 已有；版本、DAG 查询、更新、复制、移动、批量删除缺失 |
| 执行入口 | `ExecutorController` | `run start/backfill/control` 已有；batch-start、batch-execute、stream task start、execute-task 缺失 |
| 工作流实例查询 | `WorkflowInstanceController` | `instance list/get/tasks/delete` 已有；变量、甘特图、父子工作流、top-N、批量删除缺失 |
| 任务实例查询和控制 | `TaskInstanceController` | `instance task-list/force-task-success/stop-task` 已有；savepoint 未封装 |
| 任务日志 | `LoggerController` | `log detail/download` 已新增 |
| 调度 | `SchedulerController` | `schedule create/list/preview/online/offline/delete` 已有；update、按工作流 code 更新、POST list 缺失 |
| Token | `AccessTokenController` | `token create/generate/list/delete` 已有；update/user 查询缺失 |

## P0 缺口：agent 高频刚需

这些优先级最高，因为会直接影响 agent 自动建项目、建任务、排错、上线运行。

| 缺口 | Controller endpoint | 建议命令 |
|------|---------------------|----------|
| 工作流定义 raw create/update | `POST /projects/{projectCode}/workflow-definition`、`PUT /{code}` | `workflow create --task-definition-json ... --task-relation-json ...`、`workflow update ...` |
| 工作流详情和 DAG 查询 | `GET /workflow-definition/{code}`、`/{code}/tasks`、`/{code}/view-tree`、`/{code}/view-variables` | `workflow get/tasks/view-tree/view-variables` |
| 工作流版本管理 | `/{code}/versions`、`/{code}/versions/{version}`、`DELETE /{code}/versions/{version}` | `workflow versions/switch-version/delete-version` |
| Task Definition 管理 | `GET /task-definition/{code}`、`PUT /{code}/with-upstream`、`/{code}/release`、versions | `task-definition get/update-with-upstream/release/versions` |
| 调度更新 | `PUT /schedules/{id}`、`PUT /schedules/update/{code}` | `schedule update/update-by-workflow` |
| 执行高级控制 | `batch-start-workflow-instance`、`batch-execute`、`execute-task` | `run batch-start/batch-control/execute-task` |

## P1 缺口：集群配置和运行前置资源

这些接口通常是创建工作流前的前置条件。没有它们时，agent 只能使用已有环境。

| 缺口 | Controller | 建议命令组 |
|------|------------|------------|
| Tenant | `TenantController` | `tenant create/list/get/update/delete/verify-code` |
| Worker Group | `WorkerGroupController`、`ProjectWorkerGroupController` | `worker-group ...`、`project-worker-group assign/list` |
| Environment | `EnvironmentController` | `environment create/update/get/list/delete/verify` |
| Queue | `QueueController` | `queue create/update/list/delete/verify` |
| Cluster | `ClusterController` | `cluster create/update/get/list/delete/verify` |
| K8s Namespace | `K8sNamespaceController` | `k8s-namespace create/list/delete/verify/available-list` |
| Project 参数和偏好 | `ProjectParameterController`、`ProjectPreferenceController` | `project-parameter ...`、`project-preference ...` |

## P2 缺口：告警、权限、用户和审计

这些接口对生产管理重要，但不一定是最小自动化路径。

| 缺口 | Controller | 建议命令组 |
|------|------------|------------|
| Alert Group | `AlertGroupController` | `alert-group create/list/get/update/delete/verify-name` |
| Alert Plugin Instance | `AlertPluginInstanceController` | `alert-plugin create/test/update/delete/get/list` |
| 用户管理 | `UsersController` | `user create/list/update/delete/activate/info` |
| 授权 | `UsersController`、`ProjectController`、`DataSourceController`、`K8sNamespaceController` | `grant project/datasource/namespace`、`auth list` |
| 审计日志 | `AuditLogController` | `audit list/operation-types/model-types` |
| Token 完整 CRUD | `AccessTokenController` | `token get-user/update` |

## P3 缺口：监控、分析、血缘和 UI 辅助

这些更偏只读、后台或 UI 辅助，适合后续补。

| 缺口 | Controller | 建议命令组 |
|------|------------|------------|
| 监控 | `MonitorController` | `monitor servers/databases/master-executors/worker-executors` |
| 数据分析 | `DataAnalysisController` | `analysis task-state/workflow-state/commands/queues` |
| 工作流血缘 | `WorkflowLineageController` | `lineage get/list/dependent-tasks/verify-delete` |
| 动态任务类型 | `DynamicTaskTypeController`、`FavTaskController` | `task-type categories/list/favourite` |
| UI 插件 | `UiPluginController` | `ui-plugin list/get/product-info` |
| Cloud Azure DataFactory | `CloudController` | `cloud azure-datafactory ...` |
| Task Group | `TaskGroupController` | `task-group create/update/list/start/close/force-start/priority` |

## 下一轮建议实现顺序

1. `workflow` 完整 CRUD 和 DAG 查询：让 agent 能从 `task build-*` 产物直接创建任意 task DAG，而不是只有 `workflow create-shell`。
2. `tenant`、`worker-group`、`environment`：让 agent 能自建运行前置资源，不依赖已有默认值。
3. `alert-group` 和 `alert-plugin`：让调度失败通知可以自动配置。
4. `user`、`grant`、`audit`：补齐管理员自动化能力。
5. `monitor`、`analysis`、`lineage`：补齐只读诊断和治理能力。

## 设计原则

- 不要在 CLI 里重新实现 DolphinScheduler 业务规则。只封装真实 REST API。
- 对插件 schema 不做猜测。像 datasource 和 generic task 一样，优先接受原生 JSON。
- 所有新增命令必须支持根级 `--json`，失败时保持非零退出码。
- 高风险 mutation 命令默认加确认；AI 自动执行时显式传 `--yes`。
