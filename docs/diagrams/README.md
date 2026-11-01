# 项目架构与流程图集

[文档入口](../README.md) · [完整运行说明](../reference/full_run_walkthrough.md) · [打开交互图集](index.html)

Mermaid / SVG / 离线 HTML 是 **2026-10-29 版本的流程概览**；当前可编辑 Draw.io 采用 **2026-10-31 核对版**，旧同名文件已放入 [archive](archive/README.md)。各格式并非逐字同步的新版本。最新图片点策略、模块默认布局与有限覆盖补生成以 [运行说明](../reference/full_run_walkthrough.md) 为准。源码与旧设计文档有差别时，以当前实现为准；图示不代表真实模型和业务质量已验收。
图表不含密钥、私有服务地址、真实需求内容或运行数据库数据。

## 怎样查看

推荐双击 `docs/diagrams/index.html`，在浏览器中切换五张图。支持缩放、拖动画布、适应宽度、查看整图、复制 Mermaid 和下载 SVG；无需联网、启动服务或安装依赖。

| 图 | 重点内容 | 查看 / 导入 | 可维护源码 |
|---|---|---|---|
| 01 分层架构 | CLI、配置、装配、application / domain / infrastructure、端口与实际依赖 | [SVG](01_architecture.svg) | [Mermaid](01_architecture.mmd) |
| 02 完整流程 | 文档接入、图文分离、提炼、两轮 RAG、校验与联合导出 | [SVG](02_workflow.svg) | [Mermaid](02_workflow.mmd) |
| 03 RAG 展开 | 索引材料、向量缓存、空间、召回、RRF、重排、核心 / 补充证据上下文 | [SVG](03_rag.svg) | [Mermaid](03_rag.mmd) |
| 04 证据与数据流 | 原件 → 区间 → 证据 → 规则 → 点 → 用例步骤 → 覆盖与风险 → 派生视图 | [SVG](04_traceability.svg) | [Mermaid](04_traceability.mmd) |
| 05 调度与失败处理 | 有界工人、缓存、HTTP 重试、Schema 修复、批次二分、各模型失败策略 | [SVG](05_reliability.svg) | [Mermaid](05_reliability.mmd) |

另附 [五页可编辑 Draw.io 文件](tc_rag_architecture_atlas.drawio)：节点、容器和连线是原生图元，并非五张嵌入图片；可在 draw.io / diagrams.net 打开后修改。各页对应上表五张图。

## 放到你提供的 Boardmix 白板

目标：[Boardmix 白板](https://boardmix.cn/app/editor/xmNuOJdKWAK5GAPmcPxVeQ)。本次没有可用的 Boardmix 连接插件，网页读取工具也无法访问此编辑页，因此**尚未写入该白板**，没有修改白板原有内容。

最直接的查看方式：

1. 在已登录且有编辑权限的浏览器打开上述白板。
2. 在资源管理器打开项目的 `docs/diagrams/`。
3. 将 `01_architecture.svg` 至 `05_reliability.svg` 拖入画布，按编号摆放。
4. 放大阅读；建议五图分别放入五个容器，便于演示和定位。

Boardmix 官方帮助说明支持直接拖入 SVG，见 [导入说明](https://boardmix.cn/help/guide/63/1682059165518899)。SVG 是矢量查看版，不承诺导入后自动拆成可编辑的业务节点。
这些 SVG 使用白底、深色连线和纯 SVG 文字，无 HTML `foreignObject`，单边显示尺寸不超过 4096，便于导入；仍需在目标白板核对字体和布局。

如需在白板内编辑节点，当前账号若提供 Mermaid 代码编辑入口，可逐张粘贴 `.mmd` 的完整内容，不要再让 AI 改写业务结构。Boardmix 官方介绍了 [Mermaid 代码编辑与渲染方式](https://boardmix.cn/article/real-mermaid-editor-boardmix/)，实际入口和样式以当前账号界面为准。
若账号提供内嵌 draw.io，也可在该编辑器中打开 `.drawio` 文件；本次未在目标账号验证此入口或导入兼容性。需要保证外观时先使用 SVG。

## 图例与阅读约定

- 蓝色：入口、编排和主要业务处理；紫色：生成 / 视觉模型、领域契约。
- 青色：向量、检索、重排及上下文；绿色：事实保存、导出与交付。
- 橙色：可选支路、不确定性、错误处理；灰色：基础设施。
- 架构图的实线表示模块使用 / 装配关系，标注“实现协议”的虚线表示端口实现；不表示阶段执行顺序。
- 流程图实线表示正常数据或控制流，虚线表示约束、参考、重试等，按线旁文字理解。
- 图 02 的 01～17 是阅读步骤号，和 CLI 根据实际路径显示的动态阶段编号不同。

## 各阶段调用什么模型

具体厂商、名称、地址、并发和超时由 `workdir/local.yaml` 与有效配置决定，图中不固化某次运行的模型名称。

| 工作 | 模型配置角色 | 实际适配器 / 本地实现 |
|---|---|---|
| PDF / DOCX 解析、分块、规则核验 | 不调用模型 | PdfParser、DocxParser、chunking / design |
| 已提取图片转解释文字 | `models.vision`，按开关 | OpenAICompatVision；低文字量或扫描页报告未解析，不自动渲染整页图片 |
| 规则提炼 | `models.llm` | OpenAIStructuredLLM |
| 证据与原文片段入索引、两轮查询向量化 | `models.embedding` | OpenAIEmbedder；NumPy / 可选 FAISS 本地搜索 |
| 规划 / 生成候选重排 | `models.rerank`，按开关 | HttpReranker；失败沿用召回顺序 |
| RAG 测试点规划 | `models.llm` | 与提炼共用生成配置，检索后补充意图 |
| RAG + Skill 生成用例 | `models.llm` | Skill 是提示词规范，不执行脚本或写 Excel |
| 用例校验与覆盖 | 主要为本地逻辑 | 覆盖缺口触发有限补生成时再次检索并调用模型 |
| XMind / Excel 导出 | 不调用模型 | ReportExporter、XMindAdapter |

## 需要按当前实现理解的边界

1. 主线始终经过 RAG，不存在无检索生成回退，也没有中间人工审批或 XMind 回读。
2. SQLite 是唯一结构化事实源；XMind、Excel、JSON / Markdown 和索引是派生视图或产物。
3. XMind 展示策略处理后保存的全部测试点，默认按“模块/章节 → 业务维度 → 功能 → 目标”组织，以标签保留待补充/待澄清等状态；Excel 展示实际用例，两者数量不要求一致。
4. 没有就绪点也可能正常完成导出，此时 Excel 可只有表头，不能把文件存在当成有效覆盖。
5. 需求索引材料含有效引文及 Chunk 原文区间；检索限制在当前文档版本，不是自动查询全库历史需求。
6. 两轮分别用规则陈述与测试点目标查询；直接证据按规则加载，补充资料不能覆盖原文事实。
7. Vision 解释强制带不确定性；结构和引用校验通过也不证明业务语义正确。
8. 用例引用当前批次之外的 point ID 会进入现有 Schema 修复预算，耗尽仍失败；不会猜测或自动改号。

## 代码核对入口

| 说明 | 文件 |
|---|---|
| CLI、启动与覆盖 | [workflow.py](../../src/tc_rag/interfaces/workflow.py)、[config.py](../../src/tc_rag/core/config.py) |
| 适配器和 Prompt / Skill 装配 | [bootstrap.py](../../src/tc_rag/bootstrap.py) |
| 主线编排、两轮 RAG、批次和导出 | [pipeline.py](../../src/tc_rag/application/orchestration/pipeline.py) |
| 外部能力协议 | [contracts.py](../../src/tc_rag/application/ports/contracts.py) |
| 引文、约束、基础点与用例校验 | [design.py](../../src/tc_rag/application/services/design.py) |
| 图片派生证据 | [vision.py](../../src/tc_rag/application/services/vision.py) |
| 检索和索引准备 | [context.py](../../src/tc_rag/infrastructure/retrieval/context.py) |
| HTTP 重试与结构修复 | [openai_sdk.py](../../src/tc_rag/infrastructure/providers/openai_sdk.py) |
| 当前批次点 ID 契约 | [domain/pipeline.py](../../src/tc_rag/domain/pipeline.py) |

修改流程或契约时，同步维护对应 `.mmd`，并重新导出 SVG / Draw.io，避免同名图的内容分叉。

## 历史图集生成验证（2026-10-29，不代表本轮重测）

- 五张 Mermaid 均使用本地渲染器实际生成 SVG，XML 结构、文字、图形尺寸检查通过；没有 HTML `foreignObject` 标签。
- 离线浏览器实测五图切换、图片加载、源码展示、放大、100% 与整图适应均通过，JavaScript 语法检查通过。
- Draw.io 为五页原生图元，节点 / 容器 ID 唯一，连线端点存在性检查通过；尚未在 Boardmix 账号内实测导入。
- 图表已同步移除代码读取支路；本轮保留运行数据库和历史产物，未调用真实模型。
