# 固定候选池重排准备

2026-11-15。复用实际vector+BM25前40两路排名及原RRF，冻结11题×40条dev候选，原片段全文不截断，保存来源结果指纹。当前40池的宏相关片段召回为1.0，意味着当前dev正例均在池内；不表示前5能全部找到或重排必然有效。

池文件：workdir/kb/evaluation/retrieval_pilot/datasets/official_dev_rerank_pool_v1/pool.json。新增eval/kb/rerank_fixed_pool.py，默认预览，不重召回、不调用embedding/回答，--live复用bootstrap现有kb_session与HttpReranker，仅调用rank。会话仍构造已有embedding客户端但不调用。严格核对返回ID集合、重复及分数，单题错误另存，不计为正确结果或静默回退；结果JSONL逐题保存。

默认预览通过，现有配置rerank已启用。真实请求交用户显式启动；可能11次逐题rank，适配器内部行为及限额按实际记录，不声称已跑。1项Fake重排/错误集合测试通过，Ruff通过。真实适配器结果和重排评分尚未验证，test19题未运行。

下一步用户执行重排命令，助手在相同成功题集合上比较前后多k标准指标及MRR，失败单列；再冻结配置做留出验证。无真实模型调用、无新入库。
