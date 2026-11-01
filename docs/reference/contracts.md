# 领域契约参考

[返回参考导航](README.md) · [架构设计](../design/design_spec.md)

## 代码入口与校验边界

契约定义于 `src/tc_rag/domain/models.py`、`pipeline.py`、`review.py`、`rag.py`；ID/指纹位于同目录的 `identity.py`。应用服务负责原文区间、引文、跨对象引用和运行版本绑定，SQLite 进一步维护引用存在性。

领域对象拒绝未知字段，携带 `schema_version=1.0`。结构校验只证明字段满足契约，不证明对象引用存在或业务语义有依据。

### 提炼批次的对象 ID

`Extraction` 的 evidence、features、rules、entities、issues 中，对象自身的 ID 在同一批次的同类集合内必须唯一。
跨类型可以复用局部 ID：命名空间转换加入集合名，依据引用字段的类型改写，例如证据 `1` 与功能 `1` 会成为不同 ID。
关联字段重复引用同一证据是允许的；没有类型的 `Issue.object_ids` 只改写归属唯一的 ID，歧义引用保留原值，不猜测归属。
提示词建议分别使用 `e_`、`f_`、`r_`、`entity_`、`issue_` 前缀，前缀本身不是领域硬约束。

2026-10-27 将已有唯一性检查提前到 `Extraction` 的模型校验，使真实 LLM 适配器能在现有的
`generation.schema_repair_attempts` 上限内请求修复，并给出不含原始 ID 或业务正文的固定约束说明。
应用层命名空间转换仍保留同类型防御校验；不会自动丢弃对象或猜测引用。
修复耗尽仍失败；通过 ID 检查不代表引文、关联引用或业务语义已通过后续校验。

### 用例批次的测试点引用

生成时使用 `case_batch_schema(point_ids, max_cases_per_point)`，把当前批次允许的测试点 ID 绑定为运行时字符串枚举。
用例的 `point_ids` 非空且只能取这些值；引用其他批次或不存在的测试点会直接触发 Schema 错误，
由模型适配器在现有 `generation.schema_repair_attempts` 预算内修复，耗尽仍失败。
修复信息只含过滤后的字段路径、错误类型及输入契约的允许值，不回放错误模型正文。
不会按标题猜测 ID、丢弃错误用例或放宽后续规则和证据校验。

`generation.max_cases_per_point` 同时限制每个点关联的用例数量，关联多个点的用例对各点分别计数。
总数没有超过“批次点数 × 单点上限”，也不能掩盖个别点超限。请求携带 `limits.max_cases_per_point`，
Schema 校验在结构修复和有效缓存写入之前执行；超限时给出固定约束，修复耗尽仍失败。
上限与运行时 Schema 参与缓存指纹，旧的宽松 Schema 缓存不能冒充合格结果。

运行时契约沿用 `CaseBatch` / `TestCase` 的字段与严格校验，转回标准领域对象后保存；
不改变持久化结构、数据库字段或导出格式。允许值排序后进入 Schema 和缓存指纹，
不同批次以及旧通用 Schema 的缓存不能混用。此约束适用于 cosine、dot、euclidean，
与是否归一化无关。回归见 `tests/unit/test_case_scope_contract.py`、
`tests/integration/test_case_scope_repair.py`，覆盖修复成功、预算耗尽和缓存安全。

## 来源和位置

- PDF 页码和 CoverageLink.step_no 从 1 开始；DOCX 段落/表格/行列索引、块 order 和字符偏移从 0 开始。
- ChunkSource 为 Block 内 `[start_char, end_char)`；加载 Block 后还要校验是否越界。
- Locator 容纳页码、坐标及 DOCX 结构位置；允许字段缺失以记录解析局限，缺定位不能视为已可追溯。
- EvidenceSpan 的引文必须属于输入来源。vision 来源须记录 processing_model 和 processing_version，并强制 uncertain=true；origin=ocr 仅保留为历史证据兼容类型，旧数据仍须保留处理模型和版本，不代表当前提供独立 OCR 识别。
- 未知数值、单位、精度和条件保留 None，不按常识填充业务要求。

## 常用状态

| 对象/字段 | 取值与含义 |
|---|---|
| RequirementRule.status | supported / ambiguous / conflicting / needs_review |
| TestPoint.applicability | applicable / not_applicable / undetermined |
| TestPoint.status | ready / needs_context / needs_clarification / not_applicable |
| TestCase.status | draft / validated / needs_review / superseded |
| CoverageLink.validation_status | pending / validated / invalid / needs_review |
| Run.status | queued / running / succeeded / partial_failed / failed / cancelled |

这些是技术状态；validated 只表示自动检查通过，needs_review 表示有待核实风险，均不是人工批准，也不构成中间审批步骤。

## 测试用例与目录

用例至少有一步，每步同时记录 action、expected、rule_ids、evidence_ids。CoverageLink 对应具体步骤和规则/测试点，其关联存在不等于断言正确。

纯界面展示点合并在应用层保存前完成，只替换测试点目标并合并 rule_ids，不新增领域字段、不删除规则或证据；汇总点仍引用原规则和证据，XMind 只显示概括目标。

CasesRequest 只需要 run_id、生成指令和输出目录，不接受审核 ID、XMind 输入或文件校验和。
PointTree 的正式节点绑定 point_id；XMind 展示策略处理后保存的全部测试点，包含待补上下文、待澄清和不适用点。
默认 `export.point_tree_layout: module` 按模块/章节 → 业务维度 → 功能 → 目标组织，以标签保留点状态。
`feature` 为状态 → 维度 → 功能；`chapter` 在其功能层之前增加原文章节。
章节依据全部直接规则证据的共同标题路径确定，不按第一条证据猜测归属；module 布局无定位时回退功能，跨模块单列，
chapter 布局无共同路径归“跨章节”，均无标题归“未定位章节”。目录布局不删除测试点。
Excel 展示实际生成用例，二者数量不要求一致。

`planning.vision_policy` 默认 `aggregate`：普通纯图片点按功能汇总为待补文字依据的信息问题，不保留为点；
展示汇总点保留，纯图片边界点保留但设为 `needs_context + undetermined`。
`downgrade` 保留普通纯图片点并降为待补上下文，保留已有待澄清/不适用状态；`keep` 保留原点。
只有 `ready + applicable` 的点进入用例生成，策略不能把图片解释变为已确认原文事实。
历史 ReviewSnapshot 及数据库表保留以兼容旧数据，当前不读写批准记录。
详见 [目录与联合导出](../learn/P1/11_测试点目录与联合导出.md) 及 [RAG 总览](../learn/P2/README.md)。

## 质量检查

`rag quality` 只读阶段快照与运行记录，不重新解析、不调用模型、不生成或修改业务事实。
检查结果分为 `pass / warn / fail / not_applicable`：缺少阶段或未执行检查记为
`not_applicable`，不能写成通过；`fail` 只用于引用、区间、文本或快照结构被破坏以及
必需阶段缺失；启发式风险（重复、无依据数字、超预算、rerank 降级、失败调用）记 `warn`。
`semantic_review` 必须保持 `not_performed`，自动检查不构成人工批准或语义正确结论。
已有运行不回写；旧快照无法按当前契约解析时报告 `Q-SC-01`，依赖检查记 `not_applicable`。

## 身份、快照与检索

对象 ID 为带前缀 UUID4；内容指纹为规范 JSON 的 SHA-256，字典键排序、拒绝 NaN，保留列表顺序、空白及 Unicode。调用方明确选择参与字段，不自动排除 ID。文件 SHA-256 另用于字节完整性。

Run.config_snapshot 是普通数据容器，调用方须传入无密钥配置。ModelInvocation 的 usage 未知为 None，不能填 0 冒充无消耗。

`ModelInvocation.validation_errors` 是排除序列化的运行时诊断元数据，持久化到
`invocations` 阶段快照的 `validation_errors[invocation_id]`；`items` 和 model_invocations 表中的
调用 JSON 结构保持兼容。诊断只含过滤后的契约字段路径、错误类型与固定约束，不含错误输出值。
兼容序列化省略未启用的标题推断、默认 merge/aggregate 规划策略，以及旧 feature 布局值；
当前默认 module 布局显式进入快照。配置值相同也不证明代码/提示词语义相同，不能强制让旧事实适配新行为。
历史快照的处理以程序实际兼容检查为准，不能泛称所有“默认值”都不进入快照。

RAG 空间绑定服务、模型、维度、归一化与模板版本；manifest 的业务 ID/int64 映射须唯一且等长。ContextPayload 保存范围、索引版本、命中、重排、采用/省略证据及耗时。`direct_ids` 指本规则的原文依据 ID，与生成路线无关。

上下文仅使用需求文档的直接证据与检索补充资料。`source_*` 标识仍指 Chunk 字符区间对应的原文片段，不是源码引用，不因移除代码读取而删除。

历史兼容边界：`ContextPayload` 的 `code_adopted_ids` / `code_omitted_ids` 仅用于读取旧记录，
从新序列化和对象展示中排除，不进入新请求或生成流程；旧 `RagRecord.payload` 保持原样。
不增加数据库列、不改写迁移或已有运行快照。旧运行仍可 status/inspect，已有用例可用 `inspect --format xlsx` 离线重建派生文件；
旧配置不符合当前契约时，cases/resume 需要新建运行，不强行复用旧输入配置。

参考测试：`tests/unit/test_domain.py`、`test_review.py` 与 `tests/integration/test_sqlite.py`、`test_rag.py`。具体语义和业务覆盖仍须人工审核。
