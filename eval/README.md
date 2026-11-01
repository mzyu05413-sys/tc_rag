# 评估入口与检索实验

`run_eval.py` 是 `rag evaluate` 等现有 CLI 的薄入口，不是已实现的整套实验运行器。
例如 `uv run python eval/run_eval.py evaluate RUN_ID --gold 私有金标.json`。
`datasets/demo/` 目前只有占位文件，仅供未来虚构公开样例；不要放真实需求资料或密钥。

## E2 / E3 / E5 第一步：离线准备

在项目根目录执行，替换真实存在的运行 ID，输出目录必须尚不存在：

```powershell
python eval/prepare_retrieval.py --run-id RUN_ID --output workdir/dev/benchmarks/chapter6/新材料包
```

默认只读 `workdir/data.sqlite`，其他数据库使用 `--database`。工具不装配模型，不读取本地密钥配置，
不调用网络，不改动业务数据库，不覆盖已有实验目录或标注。
它从一致的只读事务恢复语料，核验索引版本、缓存空间、文本/向量指纹，
按原批次重建查询文字后再比对历史哈希；不能证明匹配时失败，禁止猜测。
缓存关闭、原材料缺失、不同空间混用等情况可能需要单独准备，不能强行复用本工具。

输出包含语料及阅读版、查询及向量、已有检索/重排记录、未标注模板、来源清单与执行脚本副本。
`corpus_vectors.npy` 为实际索引使用的 float32 精度；`corpus_vectors_cached.npy` 保留缓存的
float64 数值，`query_vectors.npy` 保留查询缓存的 float64 数值。它们共享清单中的行序与 ID 映射。
缓存若已归一化，保留更高精度也不能反推模型原始输出；不要把该文件当成原始模型响应。
`manifest.json` 最后写出，列出材料指纹、配置空间与来源状态。生成中断的无 manifest 目录不算有效材料包，
应另选一个新输出目录重试，不在该目录中补写或覆盖人工标注。
先看材料包中的 `开始这里.md`，复制 `labels.template.json` 为 `labels.json` 后人工填写。
该工作表不是 `rag evaluate` 的 gold 格式，后续指标工具需显式转换别名和字段。

## 本轮范围

- E2：已单位化缓存只能验证 cosine / dot / 平方 L2 一致性，不能冒充原始向量六组对照。
- E3：固定同一矩阵和查询，比较两后端适配器的结果与实际成本；FAISS 未安装时明确未执行。
- E5：先分析同查询、同候选的历史重排前后排名，保持原参与重排数量；不产生新服务请求。
- 业务 Recall/Hit/MRR 必须等人工相关性标注；标注未知用 null，明确无答案的查询单列。

`prepare_retrieval.py` 只负责材料准备，不计算完整评分与性能结果；不能把导出材料写成实验完成。
目录中的其他实验脚本属于独立入口，指标、标注和真实采集需分别按其使用说明验收。

## 本地网页标注

已有分组标注可生成单文件页面，不安装前端依赖，不连接模型或外部网站：

```powershell
python eval/build_annotation_ui.py --dataset workdir/dev/benchmarks/chapter6/dataset/run_0e268ec0
```

默认读取 `workdir/dev/benchmarks/chapter6/labels/`，输出
`workdir/dev/benchmarks/chapter6/annotation/index.html`，双击即可在 Edge / Chrome 中使用。
其他材料通过 `--labels-dir`、`--dataset` 和 `--output` 指定；输出文件已存在时拒绝覆盖。
页面包含私有原文，留在 `workdir/`，不提交或发布。

已人工核对的候选自动保留；助手预标只作为建议，必须明确采纳和确认。
可逐组选择相关、不相关、待定或未判断，搜索全部语料，批量确认剩余预标建议。
查询中可通过 `additional_review_groups` 指定补查组，仅作为待确认候选展示，不被批量采纳为相关。
页面优先展示“本题要找什么资料”的问句，实际检索文本可展开查看。
`labels/` 下可放 `query_questions.json`，逐条绑定源运行、record_id 和 query_id。
问句是展示提示，不进入检索文本、标签初始版本或浏览器进度身份；修改问句后刷新同一页面即可继续标注。
批量操作保留已经确认的查询以及用户明确选择的不相关、待定；不会自动声明全库审核完成。

进度暂存于当前浏览器；完成后点击“导出审核结果”保存 JSON，换浏览器前也先导出。
“导入进度”只接受同一材料与标注初始版本的本页导出文件，格式或版本不匹配时保留当前进度。
该文件保留逐组判断、候选确认和完整性审核状态，不会自动改写原始标签或业务 SQLite。
未确认查询的 `relevant_groups/relevant_corpus_ids` 为 null；未判断资料保持未知。
这是含审核状态的交换文件，须经整理和完整性检查后再用于正式评测，不能直接把草稿混入人工金标。

### 导出后的校验与历史重排评分

`reviewed_rerank.py` 提供 `validate_review()`、`normalize_review()` 和
`evaluate_history()`，用于核对导出文件与 `build_annotation_ui.build_payload()` 生成的原始种子，
展开组标签，并比较已保存的同候选重排前后排名。它们不访问网络或业务数据库。
导入前备份标签和下载原件；聊天补充判断单独记录旧值、新值、用户来源和说明，不能改写原始导出。
不要用更新后的标签覆盖仍在使用的网页材料种子。

人工候选确认不等于全库相关集合已审完。评分保留未判断项，按指标分别选择双侧可确定的相同查询：
Hit@5 可由已知正例确定；MRR@10 要求首个正例之前无未知项；nDCG@5 要求前五项判断明确，
且理想折扣增益已知（至少五个已确认相关对象，或已完成全库审核）。
全库未完成时正式 Recall@5 为 `null`，不把未知项当成负例。
结果包含每项指标的配对与排除查询、两轮 RAG 分组及历史重排耗时。

已有 `run_experiments.py` 的二值标签评分不能识别候选审核中缺失的判断，
请勿用它直接把本批部分标签当成完整金标；需要上述严格评分入口。

## 配置示例

首次配置可将 `workdir/local.example.yaml` 复制为 `workdir/local.yaml`，示例不会自动加载。
真实密钥只填 local.yaml；不要覆盖已存在配置，也不要移动数据库、资产和索引。
`parsing.infer_docx_headings` 默认 false；`export.point_tree_layout` 默认为 feature，可选 chapter。
后者仍保留状态、业务维度、功能和全部测试点，只增加原文章节层级。
修改影响解析或目录结果的配置后，须按运行快照兼容规则重新运行，不能强制让旧结果对应新配置。

离线回归覆盖完整材料包导出、源库不变、缺失/损坏拒绝、查询重建和禁止覆盖标注；
全部使用虚构 SQLite 与 Fake 向量，不访问业务模型。
