# 文档导航

[项目入口与启动命令](../README.md) · [完整运行说明](reference/full_run_walkthrough.md) · [当前质量评估](reviews/quality_review_20261101.md)

日常使用只维护 `workdir/local.yaml`；已激活虚拟环境时执行 `rag`。
框架只接收需求 PDF/DOCX，连续完成两轮 RAG 和联合导出，不读取代码仓库、不等待中间审批。

| 目录 | 读什么 | 适用场景 |
|---|---|---|
| [design](design/README.md) | 架构设计、P0～P2 模块与后续阶段规划 | 核对目标、边界与开发阶段 |
| [learn](learn/README.md) | 从领域对象到解析、生成、检索的代码讲解 | 按开发阶段学习代码 |
| [reference](reference/README.md) | 运行、目录、契约、提示词、计时说明 | 启动、排错和查询约定 |
| [diagrams](diagrams/README.md) | 架构、两轮 RAG、证据链、调度的图集 | 演示与架构讲解 |
| [reviews](reviews/README.md) | 有日期的质量评估、变更采纳与核对记录 | 审核变更、查看当时的验证证据 |
| [eval](../eval/README.md) | 实验工具、标注、指标与数据边界 | 验证检索/重排增益 |

推荐顺序：项目 README → 完整运行说明 → 架构图集 → 领域契约 → 对应学习章节。
当前流程以运行说明和代码为准；带日期的评审与归档图保留当时状态，不能代替最新行为。
学习阶段不是多套运行路线，规划功能不等于已经实现，离线检查不等于业务验收。
