# 知识库入库、问答与离线评测

以下命令在项目根目录运行。模型、密钥仍只维护 workdir/local.yaml，知识库使用 workdir/kb/runtime 下的独立数据库和资产。省略 --live 的联网入口仅展示预览。

## 入库与单次使用

此前图片处理成功、向量入库失败的恢复说明见 [向量恢复记录](records/20261112_embedding_recovery.md)。可使用 --cached-vision-only 限定图片阶段只读原缓存；缺失立即停止，向量请求仍需 --live 显式启用。

终端表现沿用原 RAG 的配置摘要、阶段编号、等待心跳和耗时汇总。单次检索与问答默认显示可读结果，原始结构化结果始终保存；--json 输出机器可读 JSON，进度写标准错误，--quiet 隐藏阶段进度。

```powershell
# 交互工作台，默认预览；由你添加 --live 启动实际模型调用
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb start
```

工作台可选文档入库、检索证据、单次问答、连续问答与查看文档。

```powershell
# 预览六份 PRD
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb ingest cordys --source-dir workdir/kb/sources/cordys

# 确认外发范围后由用户显式执行真实入库，可能调用 embedding 和已开启的 Vision
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb ingest cordys --source-dir workdir/kb/sources/cordys --live

# 查看发布的快照 ID 和文件名逻辑标识
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb list cordys

# 检索预览；增加 --live 才调用查询 embedding 和已配置的 rerank
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb search cordys -q "审批人撤回需要什么条件？"

# 引用问答与交互问答预览
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb ask cordys -q "审批人撤回需要什么条件？"
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb chat cordys

# 测试用例生成问答助手：一句问候后逐题流式输出，不显示检索阶段、分数和证据清单
kb-assistant cordys --live
# 等价的完整写法（未激活虚拟环境时）
.\.venv\Scripts\kb-assistant.exe cordys --live
.\.venv\Scripts\rag.exe kb assistant cordys --live
```

`kb-assistant` 是独立入口，等价于 `rag kb assistant`；需在项目根目录运行，配置文件与知识库目录沿用默认值。启动时先列出本次使用的配置、知识库、快照和回答提示词版本，再进入提问。assistant 使用 prompts/kb_qa/assistant_v5.md：先判断问题类型，身份、使用方式、追问等自然回答，明确要测试用例时才输出“覆盖范围 + 最多 6 条四行用例”；保留最近三轮对话（`/new` 清空），全文不超过 700 字，不扩展到加签、删除、撤回等相邻流程；检索仍走同一知识库快照与证据预算，答案以纯文本流式输出，引用序号对应实际发送的片段，证据与运行记录仍写入 SQLite。`kb start` 菜单第 6 项即此助手。省略 `--live` 只打印预览。

ingest 按目录完整成员集发布，目录删除某文件后重新导入会从新快照移除它。首次导入自动建立发布记录，无需 create 命令。CLI 使用文件名作为逻辑文档 ID，可用 --versions 指定 JSON 产品版本清单，形如 {"文件名.docx":"产品版本"}，未知填 null。知识库快照版本由程序计算。

检索支持 --snapshot 指定历史版本、重复 --document 指定文档范围、--top-k 指定数量。关键词融合与重排由现有配置启用，保留各阶段分数与降级记录。chat 在启动时锁定快照，各问题独立回答；/sources 展示本题实际证据，/new 清空当前显示记录，/exit 退出。

search 与 answer 事实先写 SQLite，JSON 为阅读产物。输出位于 workdir/kb/output/search 和 answer。CLI 批量会为每题另外保存结果，问题失败单列。

## 金标与批量执行

标注方法见 annotation_guide.md。助手将人工原文反馈转换成区间，绑定真实入库快照后再评分。首批审批流问题须使用文档范围，不能默认扩大成全库。

下面只描述格式，示例字段为占位，不构成人工金标。

```json
{"query_id":"q001","question":"问题原文","query_family":"主题组1","document_ids":["实际文件名.docx"],"snapshot_id":"实际快照ID","review_status":"pending","scope_complete":false,"should_abstain":null,"evidence_groups":null,"answer_units":null}
```

document_ids 为 null 表示全库，空列表表示空范围。evidence_groups 是“必要证据单元的列表”，每个单元内可有多个替代区间。区间字段为 block_id（准备材料中的原始 ID）、content_sha256、start_char、end_char。正式检索结果包含 original_block_id，可回映准备材料。Vision 证据另外核对 origin 和处理版本，不和原文混用。

人工完成后 review_status 为 reviewed，scope_complete 为 true。无答案题也要明确证据已查完；未知与无答案不同。答案要点为逐事实列表，should_abstain 为人工判定的布尔值。nDCG 仅在该快照全候选 corpus_grades 已完整审核，且 corpus_grades_complete=true 时计算，其他情况保留 null。分块策略改变后需要重新审核候选级相关度。

```powershell
# QUESTIONS.jsonl 中每题明确 document_ids；先预览
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb batch cordys --questions QUESTIONS.jsonl --snapshot SNAPSHOT_ID --output workdir/kb/evaluation/runs/B0 --mode retrieval

# 添加 --live 后实际逐题查询；qa 模式还调用回答模型
# 每组实验使用独立输出目录

# 已审核金标与已保存结果的离线检索评分
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb evaluate cordys --gold GOLD.jsonl --results workdir/kb/evaluation/runs/B0 --output workdir/kb/evaluation/reports/B0.json

# 人工审核回答后的离线问答评分
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb evaluate cordys --gold GOLD.jsonl --results QA_RESULTS --mode qa --reviews REVIEWS.jsonl --output workdir/kb/evaluation/reports/QA.json

# 同问题、同协议的配对差值
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli kb compare --baseline B0.json --candidate B1.json --output workdir/kb/evaluation/reports/B0_B1.json
```

回答人工审核逐条填写 run_id、review_status、correct、claim_supported、citation_supported、claim_citation_supported、answer_units_covered。各布尔列表长度分别对应回答声明数、引用数、声明数及金标要点数。存在未知时先保持草稿，评分入口拒绝将未知当负例。服务失败不记成正确拒答。

离线评分输出 JSON 与同名 Markdown，报告样本数与聚类 bootstrap 区间。簇数少于 2 时区间为空。配对差值为候选减基线，区间含 0 时只描述差值，不据此宣布优胜。无完整金标不计算正式召回；费用单价未配置，usage 未知保持 null。

## 验收边界

### 重排精确同分策略（2026-11-16新增）

默认`retrieval.rerank_tie_policy: provider_order`保持服务响应的同分次序。显式`input_order`只在分数完全相等时保留重排输入顺序；它不等于来源过滤、阈值过滤或新的召回算法。原生成和知识库HTTP适配器共享这一配置，本地YAML中启用会同时生效。

知识库固定候选池实验可使用`eval/kb/rerank_fixed_pool.py --tie-policy input_order`临时覆盖，不编辑本地YAML；未加`--live`只预检。完整命令与边界见[同分配置记录](records/20261116_tie_policy_implementation.md)。现有11题分数已做离线回放，不需为验证相同排序重复付费采集。

当前工具能执行入库、混合检索、引用问答、交互、批量、人工评分与配对统计。真实六份 PRD 尚未通过模型入库与业务指标验收。自动 DeepSeek 评审、自动标注、HTTP 鉴权与流式 API 尚未实现。知识库持久化不自动收集旧的未引用资产；崩溃后重新执行完整入库可恢复，旧活跃快照保留，未完成 job 保持原状态待人工排查。
