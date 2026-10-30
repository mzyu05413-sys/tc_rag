# 文档入口

先读 [RAG 主线总览](learn/P2/README.md)：从文档经 RAG 和 Skill 一次输出测试点 XMind 与用例 Excel，包含配置、完整命令和能力边界。只想启动或排错时看 [终端交互与调试](learn/P1/13_终端交互与调试.md)。

| 要解决的问题 | 阅读位置 |
|---|---|
| 查看详细架构、完整流程、两轮 RAG、证据链和调度图，导入 Boardmix | [交互图集](diagrams/index.html) · [图集与导入说明](diagrams/README.md) |
| 哪些目录需要维护、workdir 中的文件分别做什么 | [目录结构与文件用途](reference/directory_structure.md) |
| 一次完整运行的模型分工、架构、数据流，以及历史实测如何失败与恢复 | [完整运行过程与实测记录](full_run_walkthrough.md) |
| 框架如何分层、证据和 RAG 如何衔接 | [架构设计](design/design_spec.md) |
| 各阶段做什么、目前完成到哪里、后面如何开发 | [开发指南](design/dev_guide.md) |
| 逐步理解功能、代码和流程 | [学习导航](learn/README.md) |
| 查询字段、状态、计时口径和提示词版本 | [参考资料](reference/README.md) |

当前只有一条 RAG 流程。P0/P1 是基础模块，P2 为测试点规划和用例生成提供检索增强。保留图文解析、批量并发向量、相似度、FAISS、可选图片解释/重排和计时；已移除中间人工审核门槛。

真实需求、密钥、金标、数据库及运行产物留在被 Git 忽略的 `workdir/`。文档统一 UTF-8；不再保留失效命令、重复交付记录和未开发阶段的空学习页。
