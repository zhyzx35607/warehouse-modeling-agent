# 验证记录

日期：2026-11-06。环境：Windows，Python 3.12.10，独立 `.venv`；未加载来源目录的 `.env`。依赖现场版本为 LangGraph 1.2.20、LangChain 1.4.9、MCP 1.30.0、Pydantic 2.14.3、pytest 9.2.0。

| 检查 | 实际结果 |
|---|---|
| `python -m pytest tests -q --disable-warnings --tb=short` | 794 passed，31.80 秒；存在 670 条 warning，未作为无警告构建声明 |
| `python -S scripts/verify_demo.py` | 通过；标准库 CLI、SQLite 落表、金额篡改检测、HTTP 执行、非法输入 400、Live 端点 409 |
| HTTP 参数与产物保护回归 | 16 类非法请求返回 400 且不创建输出；内部异常固定返回 500；9 类已有文件、检查后出现的同名报告均不被覆盖，现有空目录兼容 |
| 120 订单离线执行 | 10 个模型实际落表，26 项质量检查通过 |
| `npm ci --ignore-scripts`、`npm run build` | 通过；npm 锁恢复真实 integrity，并使用公共 npm registry |
| 看板静态 HTTP | `/demo` 与构建引用的 JavaScript、CSS 均返回 200 且内容非空 |
| 浏览器实际操作 | 点击运行离线演示后显示 120 个合成订单、10 个落表模型、26/26 检查通过；实际与期望对账值均为 2015880，销售结果和表级血缘完整显示 |
| 普通图及人工中断图构建 | 两者均编译为 `CompiledStateGraph`；各 29 个业务节点，不计开始节点 |
| FastAPI 模块导入 | 成功；未启动需要 PostgreSQL 的 lifespan |
| 关键设计故障路径 | 测试覆盖模型异常、重试耗尽和熔断；严格模式不会调用程序回退或跳过设计节点 |

测试通过模拟外部适配器与阻止外网连接保持隔离。原有回归套件中涉及旧程序回退的测试显式开启兼容开关；新增严格模式测试关闭该开关，并断言失败行为。

目录链接的拒绝分支通过隔离测试；Windows 实际创建目录符号链接因权限不足而跳过，未将此用例计为真实文件系统验证通过。发布前补充的 MCP 注册相关测试 30 项通过。

本次没有执行真实模型、PostgreSQL、Doris、DolphinScheduler、Neo4j 的完整联网联调；没有测量 LLM 准确率、吞吐量或生产成本。浏览器已经完成离线演示验收，结果截图见 `screenshots/offline-demo.png`；不将离线规则演示视为 Live 模型效果验证。

本地运行产物、测试日志、`.venv`、`node_modules`、`dashboard/dist` 与 `.demo-data` 均由 `.gitignore` 排除；这些结果不是要提交的业务数据。

## v0.2 新功能验收（2026-11-06）

- 完整 Python 测试：813 passed，670 warnings，34.81 秒。
- `python -S scripts/verify_demo.py` 与 `python -S scripts/verify_lab.py`：通过。后者实际经过 CSV 上传、质量失败 422、运行历史、分页预览、复检、差异、ZIP 与路径/限额校验。
- `python -S -m warehouse_demo.evaluate`：12/12 合成案例满足预期，10 个故障被检测、2 个对照无误报；逐例定义见 `eval/quality_cases.json`，不等同于生产或模型效果指标。
- Vue 生产构建通过。浏览器实际导入五份 CSV，得到 2 订单与 10000 分实收；数据预览、历史对比和六阶段耗时已实际展示。
- CSV、运行数据库及评测生成物保存在被忽略的 `.demo-data/`；仓库只提交合成示例。

最终交叉审查修复两项可复现缺陷后，完整 Python 回归为 **815 passed，670 warnings，41.41 秒**；5 项 JavaScript 回归和 Vue 生产构建通过。两个真实 HTTP 验证脚本及 12 个质量案例再次通过。

- 合法输入产生 `10000000000000001` 分时，HTTP 导入及历史读取均保留精确十进制字符串，页面用 BigInt 显示 `100000000000000.01` 元；原始 SQLite/JSON 保留整数。测试也拒绝把已经丢精度的 JavaScript number 显示为精确金额。
- CSV 异步读取开始时清空旧输入、禁用提交，且执行函数再次检查状态；异步回归证明读取中不会提交旧 CSV，旧读取迟到也不会覆盖新选择。
- 最终 v0.2 页面截图为 `screenshots/v02-lab.jpg`。
