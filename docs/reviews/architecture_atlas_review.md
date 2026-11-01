# 架构图集核对记录

核对日期：2026-10-31。图集来自本轮评审的远端 main 提交
`ffb93dbad94760dafed2d6f50d48839cbae113b3`，已按本地契约校正。
打开 [五页 Draw.io 架构图](../diagrams/tc_rag_architecture_atlas.drawio)，可查看分层、完整流程、
两轮检索、证据链及并发/缓存/失败处理；现有 [交互图集](../diagrams/README.md) 继续保留。

| 图中位置 | 本地核对结果 |
|---|---|
| 分层与端口 | interfaces → application → domain；infrastructure 实现端口，bootstrap 装配。图中业务箭头不都代表 Python import |
| 配置与解析 | 从无密钥 local.example.yaml 建立本地配置；DOCX 字体标题推断默认关闭，显式 Heading 优先 |
| 测试点筛选 | 只为 ready 且 applicable 的点生成用例；其他点仍进入最终 XMind，不恢复人工批准步骤 |
| XMind | 全部测试点，含待澄清；默认模块/章节 → 业务维度 → 功能，feature/chapter 保留旧布局，不含步骤、不回读 |
| Excel | 实际用例及中文可读附表，新增用例追溯和质量说明，主表仍为十一列 |
| 结构修复 | 当前批次引用及单点用例上限先校验，再进入缓存；修复有次数上限，不截断或放宽规则 |
| 诊断保存 | 安全字段路径与错误类型放 invocations 阶段快照的 validation_errors 映射；不增加旧调用 JSON 字段或数据库列 |
| 终端 | 普通进度与等待心跳共享约 20 秒间隔；完成、异常、修复即时显示，短阶段仍显示毫秒 |
| 规则归并 | 保留原有保守归并；本轮不引入规则语义自动删除或 uv 镜像 |

图 02 的编号是讲解步骤，不是终端阶段编号。终端按实际运行路径编号；恢复执行、
关闭视觉等情况会改变阶段数。图中的模型均通过 models.llm / embedding / vision / rerank 配置，
没有额外代码输入或无检索生成路线。

rerank 只处理召回候选中配置的前 rerank_top_n 项，其余候选保留；失败时记录降级。
重排随上层批次并发，没有独立的并发/重试调度器。FAISS 与关键词召回按配置启用。
结构、引用和覆盖检查不能证明业务语义正确，图片解释也不能冒充已核对原文。

已校验 UTF-8 中文、五页 XML、页面内节点 ID 唯一及 source/target/parent 引用。
尚未用 Draw.io / Boardmix 客户端逐页确认排版；未上传外部网站，未调用真实模型。
