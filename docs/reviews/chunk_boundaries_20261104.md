# 分块边界修复与诊断：第一批交付

日期：2026-11-04。范围为章节/文档边界缺陷修复、真实区间重叠诊断、历史配置继承。
没有调整默认 target=900、overlap=100 或开启可选拆段；没有修改原文、Schema、提示词、数据库字段或依赖，没有真实模型请求。

## 1. 已完成的行为

1. “但是/但/除非/例外”等开头的单元只能与同文档、同完整章节的前文形成条件例外组。默认模式也遵守这一点。
2. `attach_headings` 只能把本章或祖先标题带入下一单元。上一章、兄弟小节或其他文档的标题保留为独立来源，不改写其章节。
3. 小节最小长度合并不能绕过孤立标题的归属检查；正常同父小节合并仍按现有显式开关处理。
4. `chunk-stats --compare` 未指定的选项继承历史运行，显式 CLI 参数覆盖。支持显式关闭标题衔接、按句拆段或小节合并。
5. 统计新增 `chapter_boundaries` 和 `overlap`；同时输出历史选项、实际比较选项、CLI 覆盖及 `boundary_policy=chapter_guard_v1`。

边界修复无需新增配置开关。受影响的新分块由文本与 sources 自然产生新 ID；历史 Chunk 和提炼缓存不重写。对照使用当前代码，不代表把历史算法回写到数据库。

## 2. 指标口径

- 跨一级章节：来源含至少两个已知的文档/一级章节组合；跨文档另行列出。未归类不冒充第二个已知章节。
- 重叠：同文档、同完整章节的相邻 Chunk 在同一 Block 中存在字符区间交集。不同 Block 中的同名词语不算重叠。
- 标题前缀可以属于正文的祖先章节，不因此把有效正文重叠排除。正文覆盖多个小节的合并块单独排除，不能假装归属于最后一节。
- 章节切换、跨文档、多章节混合、章节未知及来源无效分别统计，不放进有效分母。分母为 0 时比例是 null。
- 重复的交集区间先取并集再计字符数；有本地 token 计数器时计算交集文本的近似 token 分布。重复表头也算共享来源，必须结合 shared_sources.kind 解读。
- `no_shared_source_ranges` 仅说明没有共享区间，不能从快照推断“尾句超过预算”等具体原因，更不等于丢失需求。

当前指标用于诊断，不设统一的 80% 重叠硬门槛，也不代表语义边界完整率或规则召回率。

## 3. 离线复算

只读三份历史快照，分别使用历史参数和 `attach_headings=true, min_tokens=150, split_long_paragraphs=true` 比较修复前后。

| 样本 | 历史配置：修复前后块数 | 增强配置：修复前 → 后块数 | 增强配置：修复后纯标题块 |
|---|---:|---:|---:|
| 学习平台六图需求 | 14 → 14 | 13 → 13 | 0 |
| 审批评论需求 | 10 → 10 | 7 → 7 | 0 |
| 大 PDF | 122 → 122 | 79 → 94 | 16 |

三份历史参数重算结果与修复前的 Chunk 全字段相同，包括 ID。增强配置下，大 PDF 原先部分业务条目被解析为兄弟标题并强行衔接；修复后保留其章节边界，纯标题块由 0 增为 16，可能增加提炼请求。不能把“块数更少”作为正确性的替代指标；本轮没有为压缩块数修改解析器或删除标题。

历史配置的同章节重叠分别为 0/6、2/2、15/59；增强配置修复后分别为 0/6、2/2、15/61。这里按正文有效章节判断标题前缀，不同于把全部来源路径必须完全相同的旧临时脚本口径。

六个对照均验证：来源区间有效、文本等于切片拼接、非表格字符全覆盖、表格有效行全覆盖、超预算有 Issue、同输入 ID 确定、历史快照未改变。只证明结构行为，不证明模型提炼质量或真实耗时改善。

私有复算脚本与结果：`workdir/dev/maintenance/chunk-boundary-20261104/verify.py`、`verification.json`；修复前源码备份 `chunking.before.py`。未把真实需求内容放入公共测试夹具。

## 4. 文件分工

| 文件 | 用途 |
|---|---|
| `application/services/chunking.py` | 条件例外、标题和小节合并的边界防护 |
| `application/services/chunk_boundaries.py` | 新增纯计算诊断：章节、来源交集、逐对排除原因 |
| `application/services/chunk_stats.py` | 汇总诊断与重叠 token 分布 |
| `interfaces/chunk_stats.py` | CLI 三态选项、历史继承及终端展示 |
| `tests/unit/test_chunk_structure.py` | 默认/增强开关组合、跨章/跨文档/标题归属回归 |
| `tests/unit/test_chunk_boundaries.py` | 重叠分母、区间去重、同文字不同来源、未知与异常路径 |
| `tests/integration/test_chunk_stats_cli.py` | 历史继承、显式关闭、只读库与派生报告 |
| README、设计说明、分块指南及本记录 | 命令、指标、审核和局限说明 |

## 5. 用户如何审核

已激活虚拟环境时执行：

```powershell
rag chunk-stats run_9cf4c54f30b04c589fcd2b632dfacdc5 --compare
rag chunk-stats run_ee9660935a704e74b081554e27d55e41 --compare --attach-headings --min-tokens 150 --split-long-paragraphs
```

两条命令均零模型调用，只读取业务库，生成独立 `statistics.json`、`comparison.json`、`review.md`。先看统计中的排除原因，再看 review.md 的原文来源和标题归属；重点检查 PDF 新保留的 16 个标题块，不要求它们全部删除或强行合并。

## 6. 尚未包含的下一批

本节保留第一批结束时的范围记录。后续已完成可选的完整尾句、表格预分组、Word 显式表头及提炼邻文，统一审核见[第二批交付](chunk_structure_v2_20261104.md)；长段默认值未改，通用可靠 PDF 表头识别与真实模型 target A/B 仍未完成。

完整尾句 overlap、改变长段默认策略、可靠表头识别、表格预分组、提炼邻文输入契约与 target A/B 均未实现。这些属于后续策略调整，应继续保留字符来源、版本及离线/真实验证分离，不在本批悄悄启用。

回滚时撤回本批文件补丁和新增诊断模块即可；本批不需要恢复私有配置、删库、迁移或移动资产。不得整库 reset，以免覆盖同时存在的视觉修复及用户其他改动。

## 7. 工程验证与现成审核产物

- 章节/来源诊断与 CLI 专项离线测试：68 passed。
- 全量默认离线测试：`python -m pytest -q --basetemp=workdir/dev/tests/chunk-boundary-final`，**756 passed、2 skipped，90.85 秒**；跳过不计通过。
- `python -m ruff check .`：通过。`python -m mypy src`：通过，88 个源码文件。
- `chunk-stats --help`、三份历史原文的六个重算组合、两次实际 CLI 对照：通过；真实模型调用数均为 0。
- A 组默认继承审核页：`workdir/diagnostics/chunking/chapter_guard_20261104/chunk_stats_dde1ffb863f942918e54304a1a7594d7/review.md`。
- PDF 增强对照审核页：`workdir/diagnostics/chunking/chapter_guard_20261104/chunk_stats_90ec3d0b12604e438f7640a671229ad7/review.md`；同目录 statistics.json 含逐对来源交集及排除原因。

尚未验证：真实模型下的规则召回、语义完整性和费用/耗时收益，不能用上述离线通过数量替代。
