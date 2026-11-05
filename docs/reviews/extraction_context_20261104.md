# 分块第四批：子块背景窗口与提炼引用隔离

日期：2026-11-04。承接前三批，补齐长段、表格子块与需求提炼的衔接，不增加主流程阶段或人工审批。

## 1. 本批解决什么

第二批的邻文窗口按整个 Block 寻找前后文，会跳过当前 Chunk 使用的所有 Block。长段按句拆成多个子块后，同一原段剩余的条件、指代或例外没有被显式列入背景；重复表头与当前数据行之间的其他行也不会进入窗口。

需要区分：正式提炼请求的 `document_blocks` 原本就保留完整 Block，用于来源核验。此次不是宣称模型以前完全看不到这些文字，而是把当前授权区间之外的近邻明确列为背景，标出范围与用途。完整 Block 仍保留，因此不能宣称本批降低了请求 token 或解决了超长原始 Block 的请求预算问题。

本批没有改变 Chunk、领域模型、数据库字段、主提炼提示词 `extract/v2.md`、业务校验规则或默认开关。只在显式开启邻文时采用新窗口与约束提示词；没有修改 `workdir/local.yaml`、新增依赖、调用真实模型或提交 Git。

## 2. 窗口与引用契约

1. 从当前 `chunk.sources` 的字符区间并集出发，只取其外部的文字。相邻、重叠或重复 sources 不会使核心文字误入背景。
2. 先考虑同 Block 内的近邻前文、后文，再考虑同一文档的相邻可用 Block。允许跨章节提供背景，但章节规则不自动继承；不跨文档，跳过 image/other 和空文字。
3. 非连续 sources 之间的空隙可作为 `position=between` 背景。例如表头和本批数据行之间的其他行；表格优先使用真实行区间，不把 JSON 包装当成业务文字。
4. `extraction_context_blocks=N` 表示前、后、内部空隙三组**各至多 N 个片段**，总片段数最多 3N。普通完整 Block 的用法仍是前后各 N 个相邻 Block；同 Block 近邻优先占用该侧名额。
5. 全部背景受 `extraction_context_chars` 字符上限约束；按片段均分额度、取靠近核心的一端。极小预算优先保留近邻。背景可按字符窗口截取，并标 `truncated`；这不改变 Chunk 的完整句/行分块原则，也不保证每段背景都是完整原子需求。
6. 每项包含 `block_id`、`start_char/end_char`、逐字 `text`、章节、Locator、origin、模型与版本、方向、距离、`scope` 和 `role=background_only`。`scope=same_block` 或 `adjacent_block` 区分来源；距离是窗口优先级，不是字符或物理页距离。
7. **可引用范围仍仅为 chunk.sources。** 同一 Block 的其他区间也不可引用；不得将不连续区间拼成引文。原有程序校验会剔除范围外证据及依赖它的功能、规则，保留问题说明。结构校验不能证明模型对核心引文的业务解释一定正确。
8. 如果确定规则必须联合邻文证据才能成立，仍应写 Issue，不能通过背景窗口扩张依据。这一批**没有实现跨 Chunk 联合证据提炼**。

邻文约束提示词发布为 `prompts/extract_context/v2.md`，旧 v1 保留归档；仅启用邻文时加载 v2。提示词指纹和请求中的 `neighbor_context_policy=source_window_v2` 进入既有请求/版本机制，使受影响的提炼缓存按新请求区分；不改写旧快照，不能承诺旧缓存全部复用。

## 3. 两层预算与失败行为

字符窗口只控制背景文字大小，不等于 token 或最终模型上下文大小。正式提炼仍按完整请求（指令、提示词、Chunk、完整原始 Block、背景、Schema）估算预算。

预算不足先删距离较远的背景，`neighbor_context_omitted` 记录被删背景的 Block ID；同 Block 多片段可能重复记录 ID。当前 Chunk 与原始 Block 不被静默裁剪。核心仍超限时预检失败；全部提炼批次通过预检之后才发起提炼调用，避免先消耗部分额度再发现后续批次无法发送。

`extraction_context.json/md` 是**字符窗口预览**，尚未经过正式完整请求 token 预算裁减，不能当成某次模型调用的实际输入或计费数据。

## 4. 三份真实历史数据的离线回放

固定 `structural_v2 + target=900 + overlap=100 + attach_headings=true + min_tokens=150 + split_long_paragraphs=true`，邻文 N=1、总字符预算 2000。复用既有 Block，不重跑解析或图片识别。

|历史样本|Chunk 数|提供同 Block 背景的 Chunk|同 Block 背景片段|相邻 Block 背景片段|
|---|---:|---:|---:|---:|
|学习平台 Word|13|9|12|12|
|审批评论 Word|7|0|0|12|
|审批流大 PDF|96|38|38|152|

三组 Chunk 与第三批同参数产物逐对象一致；原文覆盖为 100%，背景均可按原文区间逐字回查、与本批授权区间无交集、文档与来源信息一致，原 SQLite 快照未写入。本次三份历史样本没有出现 `between` 背景，内部空隙由合成重复表头/数据行夹具覆盖，不能宣称已完成真实复杂表格验收。

以上数字是窗口结构结果，不是规则召回、业务准确率或 RAG 增益。背景可能引入额外 token 或竞争邻文名额；不提供时间/费用改善估算。

## 5. 统一审核与复现

本机四批统一入口：

```text
workdir/diagnostics/chunking/fourth_batch_20261104/README.md
```

入口链接前三批、36 组预算网格及原有待确认单元，本批三个样本均有 `extraction_context.md` 阅读版和 JSON 区间版。原标注文件没有覆盖或自动改为 confirmed。

已激活虚拟环境时，离线复现：

```powershell
rag chunk-stats RUN_ID --compare --strategy structural_v2 --target-tokens 900 --overlap-tokens 100 --attach-headings --min-tokens 150 --split-long-paragraphs --context-blocks 1 --context-chars 2000
```

按顺序核对：

1. 阅读版的“可引用正文”是否包含当前条件、行为、例外，原文区间是否正确。
2. “仅作背景”是否给出了正确指代，局部背景是否遗漏否定或限制；vision 仍标为图片解释，不能冒充原文事实。
3. 需要跨区间共同支撑的规则是否应调整分块/另做联合提炼，而不是误以为背景已有权限支撑确定规则。
4. 回到第三批待确认单元评估边界完整性；本批不代替金标或真实模型 A/B。

需要启用正式邻文时，沿用已有 `chunking.extraction_context_blocks` 与 `extraction_context_chars`；默认分别为 0、2000。诊断命令不会自动修改正式配置。

## 6. 修改索引与验证

|文件|用途|
|---|---|
|`application/services/extraction_context.py`|授权区间补集、同 Block/相邻 Block 背景、有界字符窗口与来源保真|
|`application/orchestration/pipeline.py`|在启用背景的请求中标记窗口版本；原引用校验继续生效|
|`bootstrap.py`、`prompts/extract_context/v2.md`、`pyproject.toml`|按条件装载、记录与打包新邻文约束版本；依赖未改|
|`interfaces/chunk_stats.py`、`interfaces/chunk_evaluation.py`|字符预览与阅读版，拒绝未带 compare 的显式邻文预览参数|
|`tests/unit/test_extraction_context.py`、`test_prompts.py`|区间补集、重复/相邻/重叠 sources、表格行、极小预算、非法来源、版本资源|
|`tests/integration/test_extraction_context_pipeline.py`|虚构长段实际编排；故意返回同 Block 越界证据，验证证据和依赖闭包被剔除；关闭背景兼容|
|`tests/integration/test_pipeline.py`、`test_chunk_stats_cli.py`|完整流程版本、预算预检、只读历史快照、阅读版与凭据隔离|

工程检查：完整默认离线测试 **865 passed、2 skipped（76.14 秒）**；跳过不计通过。`ruff check .` 通过；`mypy src` 通过（94 个源码文件）。三次实际 CLI 离线回放和私有核验脚本通过，模型请求 0 次。`git diff --check` 通过，仅有仓库现有换行符转换提示。

复核脚本为 `workdir/dev/maintenance/chunk-context-fourth-20261104/verify.py`，只读 SQLite，不加载配置或模型；原始文档文字仅保存在 Git 忽略的私有诊断目录。

## 7. 回退与未完成项

把 `chunking.extraction_context_blocks` 设为 0 即关闭所有邻文，或删除该可选项沿用默认；无需迁移数据库、删缓存或移动原件。若只需要原先的相邻 Block 窗口语义，需要配对撤回本批窗口实现及 v2 装载修改，不能仅改提示词版本冒充回滚。

真实提炼的漏规则、语义准确性、最终用例增益与费用仍待验证。通用可靠 PDF 表头/续表确认、跨 Chunk 联合证据提炼均未实现；本批不会将这些待做项描述为已解决。
