# 辅助建模 Agent (Assisted Modeling Agent)

本文保留原工程的架构术语和设计约定；当前个人版本的运行方式与已验证能力以 [README.md](README.md) 和 [验证记录](docs/verification.md) 为准。

一个 AI Agent，接受人类建模目标后，自主探索业务数据库、拷问人类专家补充领域知识、设计分层数仓方案、通过 Harness 校验层逐层验证、对接 Dolphin Scheduler 执行 DataX/Flink CDC 数据同步，最终将数据落地到 Doris 数仓并产出数据资产地图与知识图谱。

## 状态管理与执行载体

**ModelingTask**：
一个完整的建模任务实例。对应 LangGraph 中的一次运行（独立 thread_id / checkpoint），状态持久化到 PostgreSQL。生命周期贯穿探索→拷问→方案设计→调度执行→校验→报告的全流程。

**TaskCheckpoint**：
LangGraph 的 Checkpointer 在 PostgreSQL 中的写入点。记录任务当前所在的状态机节点、已收集的知识片段、中间产物、待处理事项列表。任务崩溃后可从最近 checkpoint 恢复。

**Blueprint**：
已验证的建模方案的可复用抽象（分层拓扑、SQL 模式、DAG 结构）。存储于 PG，带版本号。新任务启动时优先匹配 blueprint 进行适配，匹配不到再从零设计。
_Avoid_: Template, 模板

## Agent 技能体系

**Skill**：
Agent 执行子任务时可复用的行为规范单元。采用混合路线：LangGraph Subgraph 定义流程控制（状态流转、工具调用序列），Prompt 模板（Claude Code Skill 格式，YAML frontmatter + Markdown body）定义 LLM 推理逻辑与输出格式。Skill 文件存储于 PG（可版本化）。

Skill 文件见 [wren_agent/skills](wren_agent/skills)。关键 Skill 包括：`explore-schema`（数据库探索）、`grill-expert`（人类拷问）、`design-*`（各层方案设计）、`harness-validate`（校验）、`dag-deploy`（调度部署）、`blueprint-match`（蓝图匹配）、`schema-drift`（漂移检测）。

**ClarificationItem**：
Agent 在拷问人类阶段生成的待确认项。包含 question、context、candidates[]、impact_level、blocking 标记。每个项独立追踪状态（pending/answered/escalated），非阻塞项允许 Agent 继续推进，阻塞项暂停任务分支。
_Avoid_: Question, 问题

## Agent 协议与交互

**REST + WebSocket 通道**：
前端 Dashboard 和 CLI 通过统一的 REST API 做控制面操作（创建任务、提交回答、审批），WebSocket 做实时推送（Agent 思维流、SQL 执行日志、task 状态变更事件）。CLI 走同一套后端 API，WebSocket 流降级为 terminal 逐行输出。

**MCP（Model Context Protocol）**：
仅用于需要动态工具发现的场景——业务数据库探索（源库类型不确定、schema 未知）和 Dolphin Scheduler 集成（现成 MCP Server）。固定 API 的系统不套 MCP 壳，直接走 custom tool 直调。
_Avoid_: 万能集成层

**A2A（Agent-to-Agent）**：
MVP 阶段不引入。跨任务知识复用走 PG 注册册（DimensionRegistry、MetricRegistry）和 KG 查询。后续数据挖掘分析 Agent 若需要与本 Agent 协作（委托子任务、共享领域知识），再评估引入 A2A 协议。
_Avoid_: 过早引入跨 Agent 协作

## 数据分层

**ODS（原始数据层）**：
保持数据原貌，仅做物理存储，不做或仅做少量清洗。禁止跨层调用，所有下游必须基于 ODS 向上构建。
_Avoid_: Raw layer, 贴源层

**DIM（公共维度层）**：
全企业统一的维度字典（时间、地域、商品等），确保维度属性口径一致。跨 ModelingTask 共享，受 DimensionRegistry 治理。
_Avoid_: 维表

**DWD（明细事实层）**：
以业务过程为驱动，清洗、去重并集成各业务系统的明细数据。统一数据格式，结合维度做轻度宽表化。
_Avoid_: 明细层, Detail layer

**DWS（公共汇总层）**：
以分析主题为驱动，按通用维度对明细数据进行汇总，构建派生指标的汇总宽表。一致性粒度约束：同一汇总表中的度量必须具有相同的业务粒度。
_Avoid_: 汇总层, Summary layer

**ADS（应用数据层）**：
面向具体业务产品、BI 报表或数据挖掘的个性化指标。支持直接查询与高并发读取。
_Avoid_: 应用层, Application layer

**中文注释（硬性要求）**：
所有分层产出的 DDL 必须包含中文注释——表级 `COMMENT '中文'` 与字段级 `COMMENT '中文'` 缺一不可。字段有源库中文注释时直接沿用；无注释时根据列名生成中文注释（如 `amount` → `金额`、`create_time` → `创建时间`）。注释内单引号转义为 `''`。ADS 指标列同样必须带中文注释。资产地图与知识图谱产物中的表条目也须携带中文说明。

## 校验体系

**RequirementCoverage（需求覆盖声明）**：
DWS/ADS 设计对建模目标中每个请求粒度、指标及公式依赖的结构化响应。每项包含 `requirement`、`status`（supported/partial/unsupported）、物理字段或指标 `evidence`，以及未完全支持时的 `gap_reason`。禁止通过省略需求来伪装完整支持；partial/unsupported 必须进入修订或澄清流程。
_Avoid_: 需求说明, Coverage Note

**Harness 校验层**：
Agent 在每层建模方案生成后执行的自动校验框架。分三级：
- 结构校验：SQL 语法正确、表/字段存在（全自动）
- 数据一致性校验：行数对账、聚合值与明细加总一致（全自动）
- 语义校验：指标实际值与预期值对比。Agent 基于拷问阶段收集的知识生成断言，人类审核断言后 Agent 自动执行（半自动）
_Avoid_: 测试层, Test layer

## 维度与指标治理

**DimensionRegistry（维度注册册）**：
管理 DIM 层共享维度表的全局登记系统。每条维度表记录 owner（创建它的 ModelingTask ID）、全局版本号。修改约束：additive-only（只能加列，不能删/改已有列）。口径冲突通过创建维度变体解决，不覆盖旧版本。任何 DIM 变更触发所有引用该维度的 DAG 的级联校验警报。
_Avoid_: 维度管理表

**MetricRegistry（指标注册表）**：
全局统一的原子指标和派生指标登记系统。所有指标定义必须注册，命名遵循规范（含维度前缀/后缀）。Agent 在定义新指标时先查注册表，发现模糊匹配时生成对比报告提请人类仲裁。口径冲突不走自动选择。
_Avoid_: 指标字典, Metric Dictionary

## 知识表示

**KnowledgeGraph（KG）**：
Agent 的运行时认知模型，贯穿全流程（非终态产物）。存储于 Neo4j 图数据库，代码层通过 `GraphStore` 抽象接口访问。从步骤 1 开始构建，步骤 2 数据探索阶段填充，步骤 3 用于推导分层方案，步骤 4 用于生成血缘，步骤 5 直接导出为数据资产地图。节点类型包括：业务概念、指标、源表/字段、DW 表、DAG 任务、依赖关系。
_Avoid_: 元数据中心, 资产地图

**GraphStore**：
KG 的存储抽象接口，隔离 Neo4j 的具体实现细节。提供节点/关系 CRUD、路径查询、子图遍历等操作。预留未来替换或混合图存储引擎的可能性。
_Avoid_: KG Client, 图数据库客户端

## 存储基础设施

- **PostgreSQL**：主持久化存储（TaskCheckpoint、Blueprint Library、DimensionRegistry、MetricRegistry、产物版本管理、SchemaDrift 快照、审计日志）
- **Neo4j**：知识图谱专用图存储（节点、关系、血缘、依赖路径）
- **Doris**：数仓目标集群（ODS/DIM/DWD/DWS/ADS 各层数据落地）

## 数据同步

**DataX 同步**：
离线批量数据同步（T+1），Agent 通过 Dolphin Scheduler 上的 DataX 组件执行。Agent 负责生成 DataX JSON 配置文件。
_Avoid_: 离线同步

**Flink CDC 同步**：
实时/准实时数据同步，Agent 通过 Dolphin Scheduler 管理 Flink CDC 任务。校验走窗口对账（每 N 分钟统计 ODS 写入量与源库增量对比，连续 3 个窗口误差 < 阈值视为通过）。
_Avoid_: 实时同步

## 安全

**SchemaDrift（Schema 漂移）**：
源库表结构在建模任务完成后发生的变化。Agent 在每次 DAG 执行前做 schema 快照对比。Breaking change（删列、改类型）→ 暂停 + Agent 生成修复方案 + 人类审核后应用；Additive change（新增列）→ 告警不阻塞。
_Avoid_: 结构变更

**SensitiveFieldDetection（敏感字段检测）**：
Agent 在数据探索阶段通过正则/分类器自动扫描识别手机号、身份证号、银行卡等敏感字段，提示人类确认脱敏策略。
