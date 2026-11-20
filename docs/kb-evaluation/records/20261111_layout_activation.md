# 两条路线目录启用与兼容检查

2026-11-11。

新默认 project.workdir 为 workdir/rag/runtime，storage.database 为其下 data.sqlite。生成输出、重导出默认回退与 CLI 质量输出统一跟随 workdir/rag/output；分块、视觉诊断默认进入 rag/runtime/diagnostics。离线 prepare_retrieval 默认数据库同步更新，历史库可用 --database 指定。

本地 local.yaml 更新前备份到 workdir/dev/backups/local_before_rag_layout_20261111.yaml，仅路径字段修改，凭据未回显。原 workdir 根目录数据库与资产索引仍保留。历史运行恢复及质检使用旧配置，离线工具指定旧数据库。新数据库不包含历史运行，旧配置快照和索引绝对引用尚未迁移。

SQLite、模型、检索算法、生成提示词与用例编排没有逻辑变更。默认目录属于本次用户授权的行为变更。历史快照测试保留旧配置精确往返检查，同时显式验证新默认路径。

验证结果为首轮全量 pytest 864 passed、2 skipped、1 failed，唯一失败是旧默认路径断言。修订后定向复测 vision_increment 和 config 共 70 passed。ruff 对 src、受改离线工具及测试检查通过，mypy src 94 个文件通过，CLI --help 通过，diff 格式检查通过。未重跑修订后的全量测试，未进行真实模型烟测或桌面导出验收。

知识库 sources/cordys 已准备六份授权输入副本，逐文件 SHA-256 与外部原件一致。私有清单位于 workdir/kb/evaluation/corpus/intake_20261111/manifest.json。该清单只表示接收登记，版本、图表完整性、问题金标和知识库入库仍待完成。当前没有 kb 命令。

后续聚焦知识库阶段 0～1，首先完成图表与版本核查、审批流试标准备、现有检索组件复用审查和固定 Fake 基线。原生成路线后续维护仅围绕兼容问题开展。
