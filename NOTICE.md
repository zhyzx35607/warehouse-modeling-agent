# 来源、修改与许可范围

本仓库从用户已有的 Wren Modeling Agent 工作副本中选择性提取通用代码。此前个人化副本作为基础，与原始源码对比恢复了 Python 属性装饰器、依赖哈希和第三方来源信息。未携带原 Git 历史、运行时环境文件、数据库、业务任务记录、生成式业务 catalog、私有发布流水线和历史规划目录。

导入的 `wren_agent/`、原看板、原测试/评估器及迁移保留其原始来源和既有权利；原工作副本没有根 LICENSE，本仓库不把它们宣称为新创作或重新授予原本不存在的许可。

本次新增的 `warehouse_demo/`、`scripts/run_demo.py`、`scripts/verify_demo.py`、`dashboard/src/views/Demo.vue`、新增测试、说明文档和 CI 适用根目录 MIT 文本。旧代码修复不改变其来源。

`vendor/cli-anything-dolphinscheduler/` 来自既有工作副本内的第三方包，保留原始 `LICENSE`（MIT，Copyright 2026 ChenFatMan）、`setup.py` 作者字段、README 和代码。上游声明地址：`https://github.com/ChenFatMan/cli-anything-dolphinscheduler`。没有把作者替换为个人署名或占位名称。

运行依赖由 Python/npm 安装，各自许可证归对应作者。离线样例由程序生成，没有导入任何实际客户记录。
