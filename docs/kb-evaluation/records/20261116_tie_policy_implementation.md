# 精确同分保序：显式配置与适配器验证

## 交付

用户授权下一阶段开发后，将上一轮离线同分策略接入共享HTTP重排适配器，增加配置`retrieval.rerank_tie_policy`：

| 值 | 含义 | 默认 |
|---|---|---|
| `provider_order` | 分数降序，完全同分时沿用服务响应次序 | 是，保持旧行为 |
| `input_order` | 分数降序，完全同分时保留传给rerank的候选次序 | 否，显式实验选项 |

适配器直接使用返回的`index`作为同分次序依据。它不依赖片段ID、金标或正文内容，也不合并近似分数。不同分数的次序、错误处理、来源、分数、模型请求格式及费用预算路径保持既有行为。阈值仍关闭。

原生成入口与知识库入口均从bootstrap传入该选项；这是共享能力。若在公共本地配置中启用，会同时影响两条路线的HTTP重排。因此本轮不编辑`workdir/local.yaml`，知识库实验优先用脚本CLI覆盖，只影响这次实验。

配置默认值、公共YAML及本地配置示例已同步。默认`provider_order`在配置快照序列化中省略，避免未启用新策略时无意义地改变旧快照指纹；显式`input_order`会被记录。YAML→环境变量→实验CLI覆盖顺序已验证。

## 实验入口

`eval/kb/rerank_fixed_pool.py`新增`--tie-policy provider_order|input_order`。预览会显示生效策略；真实采集的每题记录和完成文件均保存`tie_policy`，完成文件同时保存候选池的`input_policy`。

仅预览，不请求模型：

```powershell
.\.venv\Scripts\python.exe eval/kb/rerank_fixed_pool.py --pool workdir/kb/evaluation/retrieval_pilot/datasets/official_dev_text_pool_v1/pool.json --output workdir/kb/evaluation/retrieval_pilot/runs/dev_text_stable_ties_v1 --tie-policy input_order
```

仍需`--live`才会请求真实rerank；本阶段不执行，也不要求用户再次付费跑同一开发集。原有采集及评分产物不覆盖。

## 验证

1. 修改共享组件前，原RAG固定Fake基线与P2适配器共31项通过。
2. 修改后，默认行为、精确同分保序、近似分数不合并、ID/文字/分数映射、配置默认快照兼容、非法参数、YAML和环境变量覆盖等相关38项通过。
3. 全量离线回归889项通过、1项跳过，耗时81.28秒。随后补充CLI预览测试及无行为变化的BM25类型注解，针对实验入口/BM25的6项测试通过；不将6项重复累加为新的全量总数。
4. Mypy通过全部119个src文件，Ruff通过本轮修改的代码和测试。全源类型检查发现BM25构造器原有未标注参数，按父类签名补齐Callable类型及返回None，无算法变化。
5. 用`httpx.MockTransport`将11题已保存分数回放到**实际HttpReranker**，候选输入保持原池次序，服务响应按旧采集次序构造；`input_order`的11题结果全部与前轮反事实排序一致。外部HTTP请求0次。回放记录：`workdir/kb/evaluation/retrieval_pilot/diagnostics/dev_text_ties_20261116_v1/adapter_replay.json`。

回放只证明实现与离线策略一致，不能证明真实服务再次返回相同分数。宏R@5=100%、MRR@5=0.894仍是同一dev分数的离线结果，不新增独立实测结论。

## 状态与下一步

同分策略已实现并验证，默认未启用。正文投影仍是评测入口策略，未自动改变正式知识库问答输入；删除线修复也尚未通过新快照发布到现有索引。

下一阶段准备未参与调参的新留出问题，先冻结正文范围、候选预算、返回k、同分策略和指标，再采集验证。新增问题须检查与既有dev/test是否共享事实或正例片段，不能仅换个问法就宣称独立留出；仍在同一固定快照时，结论只覆盖固定知识库的新问题，不代表未见文档泛化。用户采纳新金标后方能作为正式评分依据。
