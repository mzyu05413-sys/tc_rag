# 阶段 2 · DOCX 多文档入库首批实现

新增独立 IngestionService、KnowledgeRepository 端口和知识库 SQLite 适配器。复用正式 DocxParser、structural_chunks、Embedder 与 NumPy/FAISS。未改原 Pipeline 编排。当前首批支持 DOCX 文本及图片资产提取；Vision 增强、PDF 知识库装配及完整分块选项传递尚待后续补齐，配置启用 Vision 时明确拒绝，避免误认为已识别图片。

完整目录成员集一次提交，串行解析后批量 embedding，索引验证并写入内容寻址资产，SQLite 事务比较原活跃指针后发布完整快照。省略的逻辑文档从新快照移除，原快照保留。文件名是 CLI 逻辑标识，重命名视为删除再新增。同内容不同逻辑文档按版本命名空间保留独立 Block 与 Chunk。

同输入及配置稳定生成快照身份，重复导入核验向量资产后返回已有快照，不新增 embedding。重建失败保持原指针。并发任务使用 SQLite BEGIN IMMEDIATE 和期望指针比较，陈旧发布拒绝。模型请求期间不持有 SQLite 事务。失败任务记录错误类型，不记录供应商原始异常文本。可能残留未引用的内容寻址资产，后续回收机制待实现。

CLI 新增 kb ingest 与 kb list。默认 ingest 只预览，显式 --live 才访问真实向量端点；没有调用生成模型。输出路径统一 workdir/kb/runtime。CLI 首批未知产品版本保持 null，领域服务允许显式 product_versions，后续补目录版本清单。

本轮未把六份真实资料发送到模型。离线 Fake 只验证工程行为，不产生可用于业务质量评测的正式索引。现有 20 题仍待用户标注。

阶段边界：当前是阶段 2 的核心与 CLI 首批交付，完整解析选项、Vision、崩溃恢复、缓存与状态查看仍需验收。阶段 1 已有固定索引基线和原 Pipeline 回归，规范化整套产物快照尚待补齐，不能声明所有阶段门槛已完成。

验证记录：完整离线回归 800 passed、2 skipped；Ruff 与 Mypy（104 源文件）通过。随后补充 token 计数器配置传递并对入库/CLI 五项复测通过。正式解析选项未支持的配置会显式拒绝；原生成路径的检查保持独立。未运行真实 embedding、Vision 或真实业务问答。
