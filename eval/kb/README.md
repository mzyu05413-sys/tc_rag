# 知识库评测工具

2026-11-17功能任务第一阶段：`prepare_feature_tasks.py`准备任务与规则草稿；`export_generation_contexts.py`只读导出原生成运行的两轮输入证据；`score_rerank.py --scope-manifest`校验显式片段全集并提供宏微汇总。使用及审核位置见[执行计划](../../docs/kb-evaluation/feature_task_execution_plan.md)。三功能尚无完整金标，不运行正式Recall。

2026-11-17新增`prepare_holdout_candidates.py`：绑定候选问题到固定快照正文区间，检查旧正例重叠及官方正文哈希，输出待审核材料。当前10题主候选及使用边界见[跨模块记录](../../docs/kb-evaluation/records/20261117_cross_module_candidates.md)。不生成正式负例、不调用模型；下方旧条目按原交付日期理解。

R0～R3 检索实验已新增离线三模式候选运行器 `run_modes.py`；尚未交付正式金标计分和 R2 运行器。开发依据见[实验方案](../../docs/kb-evaluation/retrieval_experiment_plan.md)与[流程指南](../../docs/kb-evaluation/retrieval_experiment_workflow.md)。

## 离线三模式候选入口

实验选项 `--filter-punctuation`：查询与文档两侧过滤纯 Unicode 标点词元，仍复用原词频/RRF，默认关闭；输出记录 `token_filter` 版本。它是探索性消融，不自动替换生产检索配置。

### 五题查询向量采集

使用 `collect_queries.py`，默认只验证材料与配置空间，不装配模型客户端、不写数据库。显式 `--live` 后复用正式 embedding 适配器及缓存；不启动 Vision、回答或入库。只请求五条问题，实际 API 次数受分批和重试影响；失败后保留缓存供重试。拒绝覆盖已有输出，输出仅包含快照、向量空间、问题及向量，不导出密钥配置。

```powershell
.\.venv\Scripts\python.exe eval/kb/collect_queries.py --bundle workdir/kb/evaluation/retrieval_pilot/datasets/five_questions_v2 --output workdir/kb/evaluation/retrieval_pilot/query_vectors/five_questions_v1.json
# 用户确认后实际采集：同一命令末尾追加 --live
```

采集完成后候选对比（当前仍不输出正式 Recall）：

```powershell
.\.venv\Scripts\python.exe eval/kb/run_modes.py --bundle workdir/kb/evaluation/retrieval_pilot/datasets/five_questions_v2 --queries workdir/kb/evaluation/retrieval_pilot/query_vectors/five_questions_v1.json --output workdir/kb/evaluation/retrieval_pilot/runs/R1/five_questions_v1
```

`python eval/kb/run_modes.py --help` 查看参数。输入 `--bundle` 材料目录、`--queries` 固定查询向量 JSON、`--output` 新输出目录，可指定 `--k 5 --budget 20`。不访问模型，不加载本地密钥配置，拒绝覆盖已有输出。

查询 JSON 结构：`{"snapshot_id":"...","space":{"provider":"...","model":"...","dimension":2048,"normalize":true,"template_version":"v1"},"queries":[{"query_id":"...","question":"与材料逐字一致","vector":[...]}]}`。空间指纹必须与快照一致，不允许填写虚构空间或向量运行真实实验。

入口复用 NumpyIndex、KeywordIndex、rrf，固定文档范围、关闭重排；校验文件哈希、行序与完整索引版本。输出各路排名、向量分数、RRF 分数、原始候选来源及阶段耗时。关键词组件当前仅返回 ID，因此不输出虚构词频分数。未冻结金标时 formal_metrics 为 null。

真实中文运行需要现有 keyword extra（jieba）；缺少时明确失败，不自动安装或替换生产分词器。离线测试使用指定分词器验证真实词频和 RRF 实现，不代表真实中文效果已验收。

阶段 0 工具 prepare_corpus.py 已提供，复用正式 DOCX 解析器，输出原件、图片资产、Block 阅读版和问题清单，不调用模型，拒绝覆盖输出目录。

```powershell
python eval/kb/prepare_corpus.py --source workdir/kb/sources/cordys --output workdir/kb/evaluation/corpus/新版本
```

正式检索、问答、批量与评分入口已经注册到 tc-rag kb，使用方法见 ../../docs/kb-evaluation/usage.md。此处只保留材料准备工具，避免重复编排。

语料接收与金标准备从阶段 0 开始；检索服务及问题集就绪后实现指标计算。原工具已移除，需要的算法通过独立验收后纳入正式实现。
# 三功能基线补充入口

`score_feature_rerank.py --plan ... --runs ... --output ...`核对四组执行指纹、模型配置名、候选集合及精确同分排序后，复用固定池评分器，分批输出宏微P/R/F1、混淆矩阵、MRR和必要正文规则覆盖。服务失败排除并报告有效样本，不调用模型；两批金标不同，不合并总体指标。

`prepare_feature_rerank.py`为首批/补充批次各准备向量及混合固定池，登记原金标口径、模型和哈希；`run_feature_rerank.py --plan ... --output ...`默认全组预检，不请求模型，只有`--live`执行真实重排。复用`rerank_fixed_pool.py`，两批不合并评分；输出已存在时拒绝自动重复执行。当前材料位于`workdir/kb/evaluation/feature_pilot/datasets/two_rounds_rerank_v1`。

`compare_rrf_weights.py`接受`--run`原两路排名目录、`--gold`已发布图文金标目录、`--snapshot`固定快照文件和`--output`新目录。仅dev，固定RRF常数60/预算40，对比向量∶BM25=1∶1、2∶1，逐项复现原基线后评分，不调用检索或模型，不改生产配置。结果含排名、逐题宏微指标、混淆矩阵、必要正文规则覆盖及输入哈希。

`run_feature_baseline.py`复用现有检索组件，接受已审核的逐题完整范围金标，输出三模式、多k片段指标与独立规则覆盖。仅dev；不发起模型请求、不重入库，输出目录不得已存在。使用示例：

```powershell
.\.venv\Scripts\python.exe eval/kb/run_feature_baseline.py --bundle workdir/kb/evaluation/feature_pilot/datasets/feature_3_v1 --queries workdir/kb/evaluation/feature_pilot/query_vectors/feature_3_v1.json --gold workdir/kb/evaluation/feature_pilot/annotations/gold_v1 --output workdir/kb/evaluation/feature_pilot/runs/baseline_v1
```

