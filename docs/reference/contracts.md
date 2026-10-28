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

## 来源和位置

- PDF 页码和 CoverageLink.step_no 从 1 开始；DOCX 段落/表格/行列索引、块 order 和字符偏移从 0 开始。
- ChunkSource 为 Block 内 `[start_char, end_char)`；加载 Block 后还要校验是否越界。
- Locator 容纳页码、坐标及 DOCX 结构位置；允许字段缺失以记录解析局限，缺定位不能视为已可追溯。
- EvidenceSpan 的引文必须属于输入来源。OCR/vision 来源须记录 processing_model 和 processing_version；vision 证据强制 uncertain=true。
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

CasesRequest 只需要 run_id、生成指令和输出目录，不接受审核 ID、XMind 输入或文件校验和。
PointTree 的正式节点绑定 point_id；导出 XMind 仅列实际用例所对应的测试点。
历史 ReviewSnapshot 及数据库表保留以兼容旧数据，当前不读写批准记录。
详见 [目录与联合导出](../learn/P1/11_测试点目录与联合导出.md) 及 [RAG 总览](../learn/P2/README.md)。

## 身份、快照与检索

对象 ID 为带前缀 UUID4；内容指纹为规范 JSON 的 SHA-256，字典键排序、拒绝 NaN，保留列表顺序、空白及 Unicode。调用方明确选择参与字段，不自动排除 ID。文件 SHA-256 另用于字节完整性。

Run.config_snapshot 是普通数据容器，调用方须传入无密钥配置。ModelInvocation 的 usage 未知为 None，不能填 0 冒充无消耗。

RAG 空间绑定服务、模型、维度、归一化与模板版本；manifest 的业务 ID/int64 映射须唯一且等长。ContextPayload 保存范围、索引版本、命中、重排、采用/省略证据及耗时。`direct_ids` 指本规则的原文依据 ID，与生成路线无关。

参考测试：`tests/unit/test_domain.py`、`test_review.py` 与 `tests/integration/test_sqlite.py`、`test_rag.py`。具体语义和业务覆盖仍须人工审核。
