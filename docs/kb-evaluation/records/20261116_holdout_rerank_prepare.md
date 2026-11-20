# 留出集重排补充对照准备

2026-11-16，用户要求执行留出重排验证。现有无真实test重排结果。按此前真实调用由用户执行的约定，助手已准备19题×40条固定池和显式--allow-holdout执行/评分开关，默认dev保护保留。预览通过，未请求模型。

候选复用原留出向量+BM25排名及RRF，无新召回。声明为holdout_supplement，保留原不重排验证结果，不据新增成绩反复调参。运行前检查模型名与dev记录一致；历史未完整记录全部供应商参数，不能事后宣称所有端点参数完全同一。

池路径workdir/kb/evaluation/retrieval_pilot/datasets/official_holdout_rerank_pool_v1/pool.json。真实后续输出k5/10/20前后指标、配对有效数、错误、指纹及逐题退化/改善。当前无实测重排后分数，不填写虚构结果。
