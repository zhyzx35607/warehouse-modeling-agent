# 功能与验证地图

v0.2 将可运行的数据链路扩展为输入、执行、调查、比较、导出的完整工作台。

| 可回答的工程问题 | 已有实现 | 证据入口 |
|---|---|---|
| Agent 输出如何变成受约束的 SQL？ | Live 结构化编译器、设计校验与人工确认；本地固定契约生成 10 模型 | `wren_agent/compiler/`、`wren_agent/graph.py`、`warehouse_demo/pipeline.py` |
| 数据源异常怎样拦截？ | CSV 先全量验证表头/类型/主外键/限额，非法输入不创建输出 | `warehouse_demo/sources.py`、`tests/test_warehouse_lab.py` |
| 如何证明工具只读？ | 预览/复检以 SQLite URI `mode=ro` 打开并设置 `query_only`，测试直接 DELETE 被拒绝 | `warehouse_demo/history.py` |
| 失败如何定位？ | 保留初始检查、采样金额异常订单、计算血缘影响，逐阶段记录真实耗时 | “质量与诊断”“执行轨迹”页签 |
| 如何复现回归？ | 运行目录保存 DB、源指纹、SQL、报告；复检发现 DB 变化但不覆盖初始报告 | “重新只读检查”、完整 ZIP |
| 怎么做评测？ | 独立临时数据库注入故障，对照预先声明的期望检查 ID | `eval/quality_cases.json`、`python -S -m warehouse_demo.evaluate` |
| 如何比较版本效果？ | 比较两个保存快照的统计、检查和数据指纹；不同输入不冒充模型效果提升 | “运行历史与对比” |
| 是否有 checkpoint/人工介入？ | Live 现有 LangGraph 与 PostgreSQL checkpoint、澄清及审批流程保留 | `wren_agent/graph.py`、`engine.py`；真实外部服务联调尚未验收 |

## 五分钟演示

1. 导入 `examples/ecommerce-csv/`，打开数据预览，核对 2 笔订单及 10000 分支付。
2. 复制样例后将支付 6000 改为 5900，再导入；展示质量失败、-100 分证据及受影响模型。
3. 选择两次运行，比较 `paid_cents` 差值和 `payments` 源指纹变化。
4. 打开执行轨迹、下载 SQL/ZIP，说明每一阶段执行了什么。
5. 运行 `python -S -m warehouse_demo.evaluate`，展示 12 个固定案例及逐例期望/观测失败；解释该数据集的规模与边界。

## 已测量的口径

- v0.2 完整回归为 815 项 Python、5 项 JavaScript；这是工程测试数量。
- 本地故障评测包含 2 个无故障对照、10 个注入故障，12/12 案例满足声明的预期；10/10 故障被检测，对照无误报。这个小型合成回归集不是生产故障成功率，也不是 LLM 准确率。
- 阶段耗时来自 `perf_counter`，包括本机磁盘和调度影响；不将一次演示的数值当作性能压测结果。
- 导入数据仍使用固定电商规则；Live 模型、PostgreSQL、Doris 和 DolphinScheduler 的完整联网验证不在本次范围内。

## 取舍

CSV 契约让输入、金额粒度和失败样例可被复现。下一步要支持任意源表，应增加语义映射、数据类型和粒度约束，并复用 Live 编译器；不能仅让模型猜表名后直接执行。这里先把可核验链路做完整。
