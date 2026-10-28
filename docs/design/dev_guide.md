# tc_rag 分阶段开发指南

更新日期：2026-10-25。架构约束见 [架构设计](design_spec.md)，操作入口见 [RAG 主线](../learn/P2/README.md)。

## 1. 怎样理解开发阶段

P0/P1/P2 是同一框架的开发顺序：P0 定义基础契约，P1 建立证据与测试设计模块，P2 把向量检索接入规划和生成。当前仅运行整合后的 RAG 流程，不能将阶段当作多套运行方案。

最终使用方式：CLI 输入需求路径和提示词 → RAG 规划测试点 → Skill 与 RAG 生成用例 → 同目录输出测试点 XMind、用例 Excel 和质量报告。中间无需人工审核。底层保留文档图文解析、LLM、批量并发 embedding、可替换向量后端、多种相似度、可选 rerank、图片解释和计时。

本文的“已实现”表示代码及离线验证完成；真实模型效果、业务正确性、性能和桌面文件兼容性须独立验收。当前开发任务由用户请求确定，本文的规划不自动授权后续开发、付费调用或 Git 操作。

## 2. 阶段与当前状态

| 阶段 | 交付目标 | 当前状态 |
|---|---|---|
| P0 基础契约 | 分层、配置、领域对象、计时、样例和替身 | 基础已实现；真实业务金标由人工维护 |
| P1 证据与测试设计 | 文件 → 规则 → 测试点 → 用例和报告 | 连续编排，已移除中间审批及 XMind 回导 |
| P2 检索增强 | 向量、索引、范围检索、重排、图片和质量评估 | 2.1～2.9 代码及离线验证完成；真实效果待验收 |
| P3 可靠运行 | 可恢复任务、缓存失效、预算和故障恢复 | 待开发 |
| P4 复杂需求 | 扫描 OCR、多文档、关系确认、流程用例 | 待开发 |
| P5 规模与复用 | 基于评估调优、历史复用、API | 待开发 |

后续阶段先保留在本指南，等代码落地再增加学习章节，避免空目录与重复规划。

## 3. 开发和审核规则

每个步骤说明目的、输入输出、代码入口、失败路径和验收依据；新增代码添加必要中文注释，重点解释边界和原因。领域层保持纯逻辑，外部能力通过 ports 装配，不在生成逻辑中硬编码供应商。

每步完成后给出修改文件、相关检查结果、未验证项及人工审核点。自动检查通过不能替代业务审核。运行数据及真实文档只放被忽略的 `workdir/`；文档与测试使用虚构示例。

依赖以 `pyproject.toml` 为准，同步维护 `requirements/` 中运行、开发、可选清单并注明用途。新增迁移不改旧 SQL，提示词使用版本化文件，修改已用提示词时另建版本并记录指纹。

### 本地验证命令

在项目根目录执行；已有虚拟环境时可使用该环境的 Python：

```powershell
python -m pip install -r requirements/requirements-dev.txt
python -m pip install -e . --no-deps
python -m pytest -q
python -m ruff check src tests
python -m mypy src
python -m tc_rag.interfaces.cli --help
```

按改动范围运行必要检查。默认测试不访问模型服务；真实模型烟测需显式启用，未运行不能写成通过。不得以删断言、吞异常或放宽证据要求换取通过。

## 4. P0：基础契约

阅读 [P0 学习讲解](../learn/P0/01_功能架构与流程.md)。

| 步骤 | 已有实现与入口 | 验收及人工审核重点 |
|---|---|---|
| 0.1 仓库骨架 | `src/tc_rag/`、`pyproject.toml`、`requirements/`、`AGENTS.md` | 依赖方向清楚，依赖用途与配置不含凭据 |
| 0.2 配置加载 | `src/tc_rag/core/config.py`、`config/` | 默认/文件/环境/CLI 优先级、未知字段报错；私有 api_key 可用且不进快照/导出 |
| 0.3 领域与身份 | `src/tc_rag/domain/` 中的 models、identity、review 模块 | 版本、区间、状态、引文来源、稳定指纹与引用约定 |
| 0.4 通用计时 | `src/tc_rag/infrastructure/runtime/timer.py` | 同步/异步、失败/取消、并发隔离、累计与墙钟区别 |
| 0.5 样例与标注 | `scripts/convert_gold.py`、虚构测试夹具 | JSONL 供机器读取，人工金标可审阅；真实资料不入库到 Git |
| 0.6 CLI 与替身边界 | `src/tc_rag/interfaces/`、`src/tc_rag/infrastructure/providers/fakes.py`、`src/tc_rag/application/services/plan.py` | 预览不调用模型，替身不伪装生成结果；正式 CLI 已接真实编排 |

P0 的配置、领域和计时仍是当前实现基础，CLI 使用说明以当前实际行为为准。

## 5. P1：证据与测试设计模块

阅读 [P1 模块导航](../learn/P1/README.md)。以下路径相对 `src/tc_rag/`；完整执行仍使用 P2 RAG 主线。

| 步骤 | 输入 → 输出与主要代码 | 验收及人工审核重点 |
|---|---|---|
| 1.1 存储与迁移 | 领域事实 → SQLite；`infrastructure/storage/sqlite.py`、`infrastructure/storage/migrations/` | 迁移指纹、外键、事务回滚、来源/步骤 JSON；不把导出当事实源 |
| 1.2 接入与 DOCX | 文件 → 原件资产、Document、Block；`application/services/ingest.py`、`infrastructure/parsing/docx.py` | 内容去重、图文顺序、表格行列和未解析范围 |
| 1.3 PDF | PDF 资产 → 页码坐标块；`infrastructure/parsing/pdf.py` | 文字/表格去重、图片定位、扫描页报告、加密/损坏文件失败 |
| 1.4 清洗与分块 | Block → Chunk 和来源区间；`application/services/chunking.py` | 否定词/条件/例外保留、超长片段报告、引文回查 |
| 1.5 模型运行底座 | 结构请求 → 校验结果及调用记录；`infrastructure/providers/openai_sdk.py` | 完整输入预算、超时、重试/修复上限、鉴权失败、usage 未知值 |
| 1.6 功能与规则 | Chunk → 功能/规则/引文；`application/services/design.py` 与 Pipeline | 引文属于输入、引用闭包、重复归并与冲突；语义需人工审核 |
| 1.7 测试点设计 | 规则 → 测试意图；`application/services/design.py` | 整数上限 300 的边界为 299/300/301；未知精度/权限不编造 |
| 1.8 用例生成 | 审核点与 RAG 上下文 → 用例步骤；`application/orchestration/pipeline.py` | 每步动作/预期与证据引用；Skill 不能覆盖业务依据 |
| 1.9 校验与覆盖 | 用例 → 关联矩阵和 Issue；`application/services/design.py` | 无依据预期、无效引用、重复、未覆盖规则；自动关联不等于正确率 |
| 1.10 脑图与导出 | 用例对应测试点 → XMind 目录；事实 → Excel/报告 | 两份文件同批同目录、无步骤进入脑图、覆盖和缺漏可追溯 |
| 1.11 编排与恢复 | 阶段快照 → 一次连续运行；`application/orchestration/pipeline.py` | 规划与生成均检索；Skill 仅约束表达；有限恢复保持输入/配置一致 |

`interfaces/workflow.py` 提供交互输入、状态/快照、预览和诊断。终端操作集中见 [交互与调试](../learn/P1/13_终端交互与调试.md)，不在每个步骤重复维护命令。

## 6. P2：RAG 核心与扩展

阅读 [P2 总览](../learn/P2/README.md)。代码路径相对 `src/tc_rag/`；scripts 和 prompts 路径从项目根目录起算。

| 步骤 | 实现内容及主要代码 | 验收及人工审核重点 |
|---|---|---|
| 2.1 相似度 | `infrastructure/retrieval/metrics.py`、`scripts/bench_similarity.py` | math/NumPy 余弦一致；平方 L2 升序、内积降序；零向量/极值校验 |
| 2.2 向量空间与缓存 | `infrastructure/providers/embedding.py`、`infrastructure/retrieval/context.py` | 批量并发、响应 index、真实维度；换空间重算；损坏缓存拒绝 |
| 2.3 索引与发布 | `infrastructure/retrieval/index.py`、`faiss_index.py` | NumPy/FAISS 映射、排序一致；构建失败不发布；文件与 manifest 校验 |
| 2.4 范围检索 | `infrastructure/retrieval/context.py`、`keyword.py` | 当前文档范围；每批查询可追溯；关键词/RRF 分数独立记录 |
| 2.5 重排 | `infrastructure/providers/rerank.py` | 候选集合和顺序合法；HTTP 失败记录降级；未启用时不请求 |
| 2.6 检索增强 | `application/orchestration/pipeline.py`、`prompts/rag_context/` | 规划和生成都检索；保留核心依据；补充裁剪可审计；检索故障停止 |
| 2.7 图片证据 | `infrastructure/providers/vision.py`、`application/services/vision.py` | 图文分离、图片解释、来源/模型/不确定性、未解析范围、缓存与预算 |
| 2.8 Excel | `infrastructure/exporters/reports.py` | 十一列模板、步骤/预期对齐、可配置映射、质量页与人工客户端验收 |
| 2.9 评估 | `application/services/evaluation.py`、CLI `evaluate` | 人工语义匹配与相关性标注；缺标注为 null；自动与人工指标分开 |

OpenAI 兼容 LLM、embedding、HTTP rerank 和已提取图片的 vision 有实现。LangChain/Claude SDK 只有扩展声明，整页 OCR 和业务关系图也未实现；不能据依赖已声明就标记完成。

## 7. 当前应先做的验收

P2 工程流程接通后，先验收已有 RAG 的实际使用，再推进可靠性扩展。此处为人工检查顺序，本文整理不会自动执行真实请求：

1. 在 `workdir/local.yaml` 填实际生成、向量服务及真实维度，各模型的 `api_key` 可直接填密钥，无需终端变量；运行 `doctor` 做离线检查。留空时兼容原 `api_key_env` 方式。
2. 如需最小真实连通验证，显式执行 `doctor --live`。它只使用虚构需求，最多 8 次 LLM/embedding 请求，不覆盖 vision/rerank/用例阶段。
3. 用授权的真实需求执行一次 `start`，得到 XMind 与 Excel，抽查图片遗漏、召回条件/例外和边界；不明确内容应在问题清单中。
4. 核对 XMind 只有 Excel 用例对应的测试点，再反查“步骤预期 → 测试点 → 规则 → 原文”。检查客户端打开和列格式；自定义 Skill 应只影响生成表达。
5. 为该运行填写人工金标和相关性标注，执行 `evaluate`；查看有效覆盖、无依据预期、重复、召回与耗时/token，不能只看用例数量。

评估金标格式及命令见 [P2.9 评估与验收](../learn/P2/09_评估与验收.md)。最近代码检查记录统一见 P2 总览，文档编辑不冒充重新运行代码测试。

## 8. P3：可靠运行

尚未开发；现有快照与有限恢复可复用，不重复造存储或增加生成路线。此阶段优先解决真实使用中的失败可恢复性。

| 步骤 | 目标 | 完成标准 |
|---|---|---|
| 3.1 任务与 attempt | 显式保存任务状态、尝试、依赖和错误 | 可定位失败尝试；状态迁移可校验；重复执行不重复提交事实 |
| 3.2 缓存与失效 | 将输入、配置、提示词/Skill、模型/空间指纹纳入依赖 | 改一项只失效相关产物；不将旧事实与新文档混用 |
| 3.3 DAG 与续跑 | 连续 RAG 阶段持久化调度，支持失败节点恢复 | 恢复保持文档/配置版本一致；不重跑有效节点；检索依赖始终必需 |
| 3.4 限流与预算 | 分端点限流、共享请求预算和可解释消耗 | 并发不突破上限；无定价时费用未知；新增费用能力前不允许假估算 |
| 3.5 故障注入 | 覆盖超时、断连、坏响应、写库/发布失败、取消 | 无半成品索引、重复事实、丢失失败记录或绕过检索 |
| 3.6 变更回归 | 对输入/规则变化产出影响及覆盖差异 | 用户能审核新旧差异；不自动认定旧用例仍适用 |

## 9. P4：复杂需求

尚未开发；按真实需求暴露的缺口逐项推进。

| 步骤 | 目标 | 完成标准 |
|---|---|---|
| 4.1 OCR 与视觉 | 扫描页渲染、识别、低质量处理 | 保留页码/图片来源、模型/版本和不确定性，不能掩盖未读范围 |
| 4.2 多文档 | 显式需求集、版本、冲突与引用隔离 | 检索范围可指定；旧版本和未授权资料不混入 |
| 4.3 实体/关系候选 | 从证据提出字段、角色、状态、动作及关系候选 | 每条候选有依据与不确定性，相似分数不直接变成关系 |
| 4.4 关系确认 | 业务关系标注、版本化及查询 | 仅使用明确关系，撤销/变更后相关产物失效；不增加生成审批步骤 |
| 4.5 流程用例 | 基于确认状态和关系扩展跨功能测试 | 区分前置/条件/例外，步骤断言仍回到规则证据 |

## 10. P5：规模与复用

尚未开发；由实际数据规模和评估结果决定优先级，不为扩展而扩展。

| 步骤 | 目标 | 完成标准 |
|---|---|---|
| 5.1 重排调优 | 在已有 rerank 上评估候选量、模型和收益 | 人工标注证明召回/生成收益，同时记录延迟与消耗 |
| 5.2 近似索引 | 在精确基线之上评估近似检索 | 报告 recall、构建/查询耗时和资源，并保持 ID/版本契约 |
| 5.3 历史用例复用 | 将历史案例作为候选模板 | 历史预期不得覆盖当前需求，需核验来源、权限和适用性 |
| 5.4 HTTP API | 复用应用服务暴露任务与审核接口 | 不另建业务编排；隔离数据、权限、预算和产物路径 |

用户审核当前成果后，再按授权推进下一阶段。各阶段完成标准始终包含工程行为、真实效果和人工语义审核的区别。
