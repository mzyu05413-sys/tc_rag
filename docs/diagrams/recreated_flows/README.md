# 原图复刻与当前详细流程

交付日期：2026-11-06。[离线预览](index.html) · [三页可编辑 Draw.io](tc_rag_recreated_and_detailed.drawio)

前两页依据用户提供的截图复刻布局、文字、深色网格、节点和连线。第三页依据当前代码展开双遍视觉与两轮 RAG。各图为原生可编辑节点及连接器，未把业务图压成图片。截图缺少原文件的字体和样式元数据，不承诺不同客户端逐像素一致。

三页统一采用原截图的深色网格、深灰节点、浅灰边框与普通箭头。连线为直线或直角折线，关闭曲线、圆角和交叉跳线；背景网格使用锁定的普通图元，避免网格交叉参与跳线而形成卷曲箭头。Draw.io 页设置保存 `adaptiveColors=none`，保持指定配色，避免随编辑器主题自动反色。该属性见 [Draw.io 官方样式参考](https://www.drawio.com/docs/reference/diagram-generation/style-reference/#14-additional-mxgraphmodel-attributes)。

## 文件与打开方式

|图页|内容|可编辑文件|矢量预览|
|---|---|---|---|
|01 原图主流程|解析 → 提炼建库 → 规划/生成 → 交付|[Draw.io](01_workflow_replica.drawio)|[SVG](01_workflow_replica.svg)|
|02 原图分层架构|入口、应用、基础设施、外部资源、领域层|[Draw.io](02_architecture_replica.drawio)|[SVG](02_architecture_replica.svg)|
|03 当前详细流程|两遍视觉、两轮检索重排、引用约束、校验保存及联合导出|[Draw.io](03_detailed_workflow.drawio)|[SVG](03_detailed_workflow.svg)|

在 draw.io / diagrams.net 中选择“文件 → 从设备打开”，打开三页图集，底部页签切换三张图；也可分别打开单页文件。编辑完成后保存为 `.drawio`，无需重新生成。背景网格在独立图层中，可在“图层”面板隐藏。

直接双击 [index.html](index.html) 可离线预览：切页、滚轮缩放、拖动平移、整图或适应宽度。第三页建议先“整图”了解主线，再“适应宽度”阅读细节。预览没有外部脚本、CDN 或网络模型请求。

## 前两张图如何理解

复刻页保留原图文字，例如 `DeepSeek`、概括性的“检索与重排（可选）”和原分层图例；这些是原图表达，不能据此推断当前运行必然使用某个厂商，也不能理解为主线允许关闭 RAG。当前实现以第三页及源码为准：向量检索参与两轮，rerank 按配置可选。

## 第三张图的阅读顺序

1. **A 接入解析**：CLI 输入路径与目标，保存原件及指纹，PDF/DOCX 图文分离，保留 Block、章节和 Locator。
2. **B 双遍视觉**：先检查图片大小和缓存；未命中时提供总览与原像素局部图、同章节正文。第一遍主读生成结构化草稿；第二遍重新读取图像、上下文和草稿复核，再做结构与引用检查。失败以图片为单位记录 Issue，保留原图和原因。
3. **C 提炼建库**：结构分块，LLM 提炼 Feature、Rule、Entity、Evidence、Issue；校验授权区间和引用闭包。有效引文与 Chunk 原文区间向量化，SQLite 保存事实，NumPy/FAISS 保存派生索引。
4. **D 第一轮 RAG**：先生成可确定的本地基础点，再按规则批次查询、检索、可选重排，结合直接证据和补充资料规划测试意图，归并保存 PointTree。
5. **E 第二轮 RAG**：筛选就绪且适用的点，准备或复用索引；按测试点批次再次查询、检索、可选重排，结合规则证据与 Skill 生成用例，做引用、风险、重复及覆盖检查，按预算有限补生成。
6. **F 联合交付**：保存事实并输出全部测试点 XMind、实际用例 Excel、质量/覆盖和调用报告。无就绪点时也可能输出目录与问题，Excel 仅有表头；文件存在不代表覆盖合格。

## 配置与模型角色

|位置|模型 / 配置|图中需要注意的行为|
|---|---|---|
|视觉第一遍|`models.vision`、`prompts/vision/v2.md`|总览与局部视图在同一次请求中提供，不是每个裁片单独生成一段再拼业务逻辑|
|视觉第二遍|`models.vision_review`、`prompts/vision_verify/v1.md`|独立复核端点启用时校验其与主读不是相同端点及模型；未启用独立端点时适配器可复用主模型|
|复核触发|`parsing.vision.verify`|true 执行；auto 按类型、局部图、不确定性等条件触发；false 跳过；缓存命中不重新调用|
|复核失败|`parsing.vision.verify_failure`|默认 fail 将该图记为失败；可选 keep_draft 只保留第一遍结构及引用有效的草稿，明确标记未复核|
|规则提炼、测试点规划、用例生成|`models.llm`|各阶段使用各自版本化提示词；Skill 是内容规范，不负责生成文件或执行脚本|
|入索引及两轮查询向量化|`models.embedding`|批量、并发、缓存；向量与索引空间保持一致；本地检索不是模型推理|
|两轮重排|`models.rerank`、`retrieval.rerank_top_n`|对召回候选的前 N 项重排，再接回其余候选；失败保留召回顺序，并非只保留 N 项|
|候选量|`retrieval.vector_top_k`|按当前配置与实际可召回内容决定；图里不固化某次实验的 20/50 等数值|

视觉第二遍不是人工审核，也不是从图片“证明”隐藏业务逻辑。结果继续标为 `origin=vision`，`human_verified` 仍为 false。同一图的主读与复核串行，不同图片之间可以有界并发；同一轮 RAG 内也可以按批并发。

两轮查询分别基于生成目标与规则陈述、生成目标与测试点目标。直接证据按规则关系加载，不依赖向量召回是否命中；检索补充资料按预算裁减，不能凭相似度新增规则或扩大引用范围。索引与向量缓存可以复用，不能把两轮等同于整篇文档向量化两遍。

## 代码核对与验证边界

|文件|核对内容|
|---|---|
|[pipeline.py](../../../src/tc_rag/application/orchestration/pipeline.py)|接入、提炼、索引、两轮查询、补充上下文、就绪筛选、覆盖补生成与导出|
|[vision.py 应用服务](../../../src/tc_rag/application/services/vision.py)|逐图并发、缓存、图片派生 Block、失败 Issue|
|[vision.py 适配器](../../../src/tc_rag/infrastructure/providers/vision.py)|主读与复核请求、Schema/引用检查、复核触发和失败策略|
|[domain/vision.py](../../../src/tc_rag/domain/vision.py)|facts、relations、tables、context_links、uncertainties 及引用契约|
|[retrieval/context.py](../../../src/tc_rag/infrastructure/retrieval/context.py)|向量查询、可选关键词融合、重排候选保留与降级、直接/补充证据分离|
|[bootstrap.py](../../../src/tc_rag/bootstrap.py)、[config.py](../../../src/tc_rag/core/config.py)|模型装配、独立复核模型约束、各开关与预算|

本次为文档图件工作，不修改业务代码、配置、密钥或运行事实，不调用真实模型。未把当前尚未实现的跨 Chunk 联合证据提炼、通用可靠 PDF 续表确认画成已实现能力。

已完成的检查：三张 SVG 在本机 Chrome 中渲染核对；原生 XML 的图页数量、ID 唯一性、连线引用、单页与图集内容一致性、画布边界、SVG 资源及文档链接均通过；`ruff check .` 通过。业务代码未变更，本次未重复运行 pytest/mypy，未调用真实模型。

尚需用户确认：在自己的 draw.io 客户端导入图集，检查字体显示并试改一个节点和连接器；重点核对第三页两遍视觉的复核开关、失败策略，以及两轮 RAG 的直接证据与补充资料边界。尚未在用户的 draw.io 客户端或 Boardmix 账号内完成导入验收，图件结构合法也不代表业务质量已通过。

## 可重复生成

已激活虚拟环境时运行：

```powershell
python scripts/diagrams/build_recreated_flows.py
```

生成器只依赖 Python 标准库，会更新本目录七个 Draw.io/SVG 文件，不会更新 HTML/README。手工编辑 `.drawio` 后请另存副本；再次运行生成器会覆盖同名生成文件。已有的五页图集和 archive 均未替换。
