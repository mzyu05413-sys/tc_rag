# 阶段 1 · 复用审查和版本契约

用户确认六份 PRD 来自不同产品时期，金标由用户人工标注。产品业务版本、文件内容哈希和知识库快照版本分别保存，未知产品版本保持 null。允许不同模块的不同时期文档组成显式快照；冲突问题评为有争议或待澄清，不自动选择所谓最新业务事实。

新增 domain/kb 的 DocumentVersion 与 KnowledgeSnapshot，纯数据验证，无 I/O。一个快照中同一逻辑文档只选一个内容版本，版本 ID 全局唯一；同内容可由不同逻辑文档引用，不强制哈希去重。

复用清单如下。

| 服务 | 决策 |
|---|---|
| DocxParser、PDF 解析、LocalArtifactStore | 保留正式实现，新入库显式指定独立资产根目录 |
| structural_chunks | 复用算法及 ChunkSource，检索片段映射回原始 Block |
| Embedder、EmbeddingSpace | 复用端口，缓存按向量空间及文本指纹隔离 |
| NumpyIndex、FaissIndex | 复用 build/search，按快照发布，allowed_ids 在 top-k 前过滤 |
| KeywordIndex、rrf、Reranker | 复用召回与重排，保留独立分数和降级记录 |
| RagContext | 当前 prepare 绑定 scope、证据和运行状态，不能连续传六份文档冒充知识库；新应用编排显式管理快照 |
| build_pipeline、validate_runtime_config | 保留原生成必需模型校验，新仅检索装配入口不能直接调用完整生成装配 |
| SQLite | 知识库独立路径，阶段 2 新仓储需提供事务发布与幂等，通用 save 接口不足以单独保证发布一致性 |

阶段 2 拟新增 application/kb 入库服务及 infrastructure/storage/kb 仓储。输入含 kb_id、逻辑 document_id、文件、可空 product_version 和解析/向量配置；输出含版本、状态和快照。失败保留旧发布指针。查询输入显式绑定 kb_id、snapshot_id、可选文档 ID 及 query；过滤请求超出快照拒绝，不静默扩大范围。

本阶段采用已有虚构 Pipeline/RAG/CLI 用例验证兼容，固定来源/向量/排名快照测试进一步补充。当前阶段不进行真实模型调用。新领域契约不被原 Pipeline 导入，现有命令语义保持原状。

本轮验证：新领域契约及原 Pipeline/RAG/CLI 关键测试共 40 项通过，Ruff 通过，Mypy 检查 96 个源文件通过。另新增固定矩阵的排名、分数、过滤、资产哈希和索引重载基线。原入口全链路的规范化产物快照仍待补全，阶段 1 尚未整体验收。
