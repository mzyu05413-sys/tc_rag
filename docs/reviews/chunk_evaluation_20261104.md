# 分块第三批：预算实验、原文覆盖与需求边界评估

日期：2026-11-04。继续[第一批章节边界](chunk_boundaries_20261104.md)与[第二批分块策略](chunk_structure_v2_20261104.md)，本批补齐离线评估能力和统一审核材料。

后续第四批已补齐子块背景窗口与提炼隔离，最新四批统一入口见[第四批交付](extraction_context_20261104.md)；本记录保留第三批当时的范围和数据。

本批没有更改正式生成流程、领域模型、默认配置、提示词、数据库字段或依赖。`workdir/local.yaml` 未改，没有真实模型请求，没有提交 Git。

## 1. 已完成什么

1. `chunk-stats --compare` 支持独立覆盖 target/overlap，未指定时仍继承历史配置；原统计保留历史预算，试分块另记实际预算。
2. 支持有限预算网格，同一历史 Block 和计数器比较多个组合，保存每组 Chunk、Issue、结构检查、近似 token 和本地分块耗时。
3. 补充精确区间覆盖检查，不再把“这个 Block 曾出现”作为全文未丢失的充分条件。
4. 提供独立的需求边界抽样标注格式、候选阅读版和评分，只有人工 confirmed 的原子需求进入边界完整率分母。
5. 已完成三份文档、两种策略、六档预算组合共 **36 组离线对照**，生成 36 项待确认候选；同文档两策略的候选一致，只标一次。

新能力只属于诊断和实验入口，不增加主流程步骤，也不恢复运行中的人工审批。

## 2. 指标如何区分

| 指标 | 计算依据 | 可以说明什么 |
|---|---|---|
| 原文字符覆盖 | 应保留区间中被有效 Chunk 覆盖的字符数 / 全部应保留字符数 | 检查丢字、漏行；不能证明语义完整 |
| 相邻实际重叠 | 同文档、同完整章节的有效相邻对中，存在同源区间交集的对数 / 有效对数 | 共享上下文的结构诊断；包含重复表头，不是需求召回 |
| 抽样边界完整率 | 全部来源范围能完整落在某个 Chunk 内的 confirmed 单元数 / confirmed 单元总数 | 这份人工抽样中的条件、行为、例外是否被分散 |
| 分散但未丢失的单元数 | 全部 Chunk 联合覆盖完整，但没有任何一个 Chunk 单独完整覆盖的 confirmed 单元数 | 区分“切散”与“丢失” |
| 缺失单元数 | 所有有效 Chunk 合起来仍无法覆盖完整的 confirmed 单元数 | 抽样约束存在未覆盖区间 |
| 分块文字估算 token | 所有 Chunk 的 token_estimate 求和 | 当前计数器下的文字体量；不含完整原文、提示词、Schema、邻文，不是实际计费 |
| local_chunking_ms | 本机一次结构分块的墙钟耗时 | 仅本地开销，不是模型/全流程耗时；未做独立预热或重复性能分位统计 |

没有有效分母时返回 null。未标注、pending、excluded 都不能冒充已确认金标。边界完整率按来源的精确区间计算，不使用词面相似度；不同 Block 即使文字相同也不能替代。

对段落与标题检查全部原始字符；对表格检查每条完整数据行的真实 JSON 区间，包装字段、外层括号和逗号不强制重复进入 Chunk。无可定位行的表格回退检查整块，不静默忽略。

来源缺失、区间越界、指向 image/other、重复 Chunk ID、正文不等于来源切片拼接的 Chunk，不参与有效覆盖。超预算而未报告 Issue 也会令结构检查不通过。跨章统计沿用前两批的独立诊断。

## 3. 标注契约

这是分块评估格式，与规则金标、检索相关性标注、视觉审核 JSON 不同，不互相冒充。

```json
{
  "schema_version": "1.0",
  "run_id": "run_xxx",
  "block_fingerprint": "对应历史 Block 集的指纹",
  "scope": "sampled_atomic_requirements",
  "units": [
    {
      "unit_id": "U001",
      "status": "pending",
      "description": "确认是否为一条完整可测试约束",
      "sources": [
        {"block_id": "b1", "start_char": 0, "end_char": 5, "quote": "符合条件。"}
      ],
      "note": ""
    }
  ]
}
```

示例只说明结构，不是可以对任意运行直接使用的有效金标。正式文件由 `--prepare-gold` 根据当前历史原文生成。

- `pending`：尚未确认是否为完整原子需求，评分时不计。
- `confirmed`：人工已确认这是一条完整可测试约束；按所有 sources 联合判断，不允许跨文档。
- `excluded`：不是本次范围内的需求单元，保留排除理由；不能只因需求描述 UI 展示就排除它。

候选从原始段落、条件例外组和表格行中确定性取样，独立于策略和 target/overlap。它们可能包含多条约束、表头或需补充的条件，**不是自动提炼好的原子需求**。审核时可拆分 ID/范围、补上同文档其他来源或排除，quote 必须与 `[start_char,end_char)` 逐字一致。

标注绑定 run_id 和包含 ID、正文、Locator、origin 等信息的 Block 指纹。错运行、错版本、重叠/重复区间、未知字段、重复单元 ID 和不一致引文均拒绝，不会为了评分静默跳过。输入标注文件只读，派生副本写到新的诊断目录。

`confirmed` 只是评估单元状态，不是规则批准状态。vision 仍是模型解释，评分按 origin 分组保留文字、视觉和混合来源；确认分块单元不等于证明图片解释已成为业务事实。

## 4. 36 组实际对照

统一设置：标题衔接 true、小节阈值 150、按句拆段 true；变量为 strategy={structural, structural_v2}、target={600,900,1200}、overlap={0,100}。使用历史 Block 和计数器，未重解析文件或重跑视觉。

以下列出 overlap=100 的块数，完整 36 组见私有审核入口：

| 文档 | target | structural 块数 | structural_v2 块数 |
|---|---:|---:|---:|
| 学习平台 Word | 600 | 17 | 19 |
| 学习平台 Word | 900 | 13 | 13 |
| 学习平台 Word | 1200 | 9 | 9 |
| 审批评论 Word | 600 | 10 | 11 |
| 审批评论 Word | 900 | 7 | 7 |
| 审批评论 Word | 1200 | 7 | 7 |
| 审批流大 PDF | 600 | 125 | 132 |
| 审批流大 PDF | 900 | 94 | 96 |
| 审批流大 PDF | 1200 | 75 | 75 |

36 组都通过来源合法、文字可重建、原文字符/数据行覆盖、超预算告警和稳定 ID 检查；原文覆盖均为 100%。历史快照与派生 comparison.json 中保存的原始 Block/Chunk 一致。

36 项候选全部 pending，**所有组合的人工边界完整率均为 null**。尚不能断言 target=1200 更好，或 600 虽然块多但更准。重复上下文可能增加块数和文字量，块更大也可能影响提炼召回，这些需标注和后续模型实验确认。

旧 structural 即使 overlap=0，大 PDF 仍出现实际共享来源，例如 target=900 时 13/61 对，来自表头等重复来源。`overlap_tokens` 是普通打包的尾部预算，不是禁止一切重复资料的开关。

v2 的 PDF 续表取舍仍与第二批相同：停止注入未确认表头并保留疑点；超预算变少不能自动解释成语义质量提高。通用可靠 PDF 表头确认与跨 Chunk 联合证据提炼仍未实现。

## 5. 统一审核与命令

三批统一入口：

```text
workdir/diagnostics/chunking/third_batch_20261104/README.md
```

该入口包含三份文档的两组预算表、逐块对照，以及每份文档**只需审核一次**的 12 项候选阅读版和 JSON。原文区间/规则语义仍由你最终审核；本批不要求现在逐项回复。

已激活虚拟环境，生成离线网格与待确认候选：

```powershell
rag chunk-stats RUN_ID --compare --strategy structural_v2 --attach-headings --min-tokens 150 --split-long-paragraphs --targets 600 --targets 900 --targets 1200 --overlaps 0 --overlaps 100 --prepare-gold
```

只比较一个预算，并使用同运行已审核的标注：

```powershell
rag chunk-stats RUN_ID --compare --strategy structural_v2 --target-tokens 1200 --overlap-tokens 100 --gold 私有标注.json
```

未显式给出的标题/小节/拆段选项仍继承历史配置；要复现上表，需带上第一条中的相应开关。参数不在命令里写明，不会偷偷继承今天的私有 YAML 分块参数。

`--targets`、`--overlaps` 可重复，去重后做完整笛卡尔积，上限 32 组；所有组合预先校验 `0 <= overlap/min_tokens < target`，非法组合整组失败，不跳过。预算覆盖/网格须带 `--compare`。`--gold` 与 `--prepare-gold` 互斥，已存在标注不会被模板覆盖。

输出：statistics.json 保留历史与试分块统计，budget_matrix.json/md 保存每组完整 Chunk 和对照指标，gold.review.json/gold_review.md 保存标注副本或待确认候选。没有候选标注时不自动给出边界评分。

## 6. 代码与测试审核索引

| 文件 | 本批用途 |
|---|---|
| `application/services/chunk_coverage.py` | 来源区间索引、区间并集/差集、逐字/逐行覆盖与结构诊断 |
| `application/services/chunk_gold.py` | 抽样标注格式、原文版本校验、待确认候选与边界完整率 |
| `application/services/chunk_experiments.py` | 有界预算网格、同原文对照、稳定 ID 检查；不自动选参数 |
| `interfaces/chunk_evaluation.py` | 派生 Markdown / JSON 阅读版输出 |
| `interfaces/chunk_stats.py` | 预算覆盖、网格、标注输入和只读 CLI 集成 |
| `tests/unit/test_chunk_evaluation.py` | 字符缺口、表格行、坏来源、区间集合独立校验、人工状态与版本约束 |
| `tests/unit/test_chunk_experiments.py` | 非法/超量网格、去重、同原文复算和不自动选择 |
| `tests/integration/test_chunk_stats_cli.py` | 历史预算保留、零网络、标注只读、错误预算不留下半成品 |

范围限于离线诊断，没有修改原质量报告中的 Q-CH 检查、生成校验或提示词。旧检查仍按既有口径运行，本批更严格的字符覆盖结果在诊断产物中独立呈现。

工程验证：

- `python -m pytest -q --basetemp=workdir/dev/tests/chunk-evaluation-final`：**828 passed、2 skipped，71.66 秒**，跳过不计通过。
- `python -m ruff check .`：通过。
- `python -m mypy src`：通过，94 个源码文件。
- 六次实际离线 CLI、36 组预算对照、三份标注候选的策略间一致性与原 SQLite 快照对照：通过；模型调用 0。

私有复核脚本为 `workdir/dev/maintenance/chunk-evaluation-20261104/verify.py`；其汇总不含配置凭据，原始需求文字仅留在被 Git 忽略的诊断材料中。

## 7. 回退与未验证项

本批没有默认启用项。继续使用原来的 `rag` / `chunk-stats` 命令即可；不用删除或移动任何原件、数据库或索引。若撤回代码，只撤回本批五个诊断模块的新增/修改及相应测试和文档，保留前两批和用户其他改动，禁止整库 reset。

待最终审核：候选是否为完整原子需求、跨段条件和例外是否齐全、表格列语义、不同预算下单元完整情况。审核后可用同一份金标重算两种策略，无需模型调用。

尚未验证：真实模型提炼的规则召回、第二轮 RAG 和用例质量、最终费用与全流程耗时。不自动跑付费 A/B，也没有把 36 组离线实验写成端到端业务验收完成。
