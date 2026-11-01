# main 更新采纳与用例数量超限修复

日期：2026-10-31。以本地实现为基础，选择性移植已评审的 main 提交
`ffb93dbad94760dafed2d6f50d48839cbae113b3`。用户明确排除规则语义去重与 uv 镜像。
没有直接覆盖或合并整个远端提交，没有提交或推送 Git，没有调用真实模型。

## 本轮交付与审核文件

路径相对项目根目录；表中列出本轮直接维护文件，不把共享工作区其他会话的实验、质检、分块、
提示词及文档整理改动算成本轮新增功能。

| 内容 | 主要文件 | 需要审核的点 |
|---|---|---|
| 安全结构诊断 | `src/tc_rag/infrastructure/providers/openai_sdk.py`、`application/progress.py`、`domain/models.py`、`bootstrap.py` | 过滤字段路径/类型/固定约束，未知字段不回显；调用 JSON 不加字段，诊断另存现有快照 |
| 配置错误提示 | `src/tc_rag/interfaces/workflow.py` | 只显示契约字段及固定原因；配置错误与领域数据错误分开，不显示输入值 |
| 终端节流 | `src/tc_rag/interfaces/progress.py` | 普通进度/心跳共享约 20 秒间隔；完成、失败、修复即时，毫秒显示保留 |
| DOCX 标题推断 | `src/tc_rag/infrastructure/parsing/docx.py`、`core/config.py`、`bootstrap.py`、`interfaces/workflow.py` | 默认关闭；格式继承、显式 Heading 优先、正文局部强调不误判；estimate 与正式解析使用同一开关 |
| 可选章节目录 | `src/tc_rag/application/orchestration/pipeline.py`、`core/config.py` | 默认状态 → 维度 → 功能；chapter 增加共同章节；跨章/未定位独立分组，全部点保留 |
| Excel 追溯与质量说明 | `src/tc_rag/infrastructure/exporters/reports.py` | 主表十一列及中文摘要保留；新增用例追溯与质量说明，区分自身风险和共享来源问题 |
| 离线材料准备 | `eval/prepare_retrieval.py`、`eval/README.md` | 只读一致事务、缓存/索引/空间/查询指纹核验、不覆盖标注；导出材料不等于实验评分 |
| 无密钥配置示例 | `workdir/local.example.yaml`、`.gitignore`、`config/defaults.yaml`、`config/example.yaml` | 仅无密钥示例可跟踪，真实配置与运行数据继续忽略；默认不开启标题推断或章节布局 |
| 架构图与契约说明 | `docs/diagrams/tc_rag_architecture_atlas.drawio`、`architecture_atlas_review.md`、`contracts.md`、`timing.md` | 五页原生图已按全量 XMind、诊断快照与有界修复校正，客户端排版待核对 |
| 文档入口 | `README.md`、`docs/design/`、`docs/reference/full_run_walkthrough.md`、`directory_structure.md`、`docs/learn/P1/13_终端交互与调试.md` | 首次配置、开关、字段诊断与当前流程一致，不恢复代码输入、OCR 或人工审核门禁 |

这里 `application/`、`domain/`、`core/`、`interfaces/` 的省略路径均位于 `src/tc_rag/` 下。
未新增依赖或数据库列；pyproject.toml、uv.lock、requirements 三份清单没有本轮变动。
现有保守规则归并、数值边界、批次引用、PDF 局部图片及全量点目录保护继续保留。

## 本次截图为什么失败

运行：`run_ee9660935a704e74b081554e27d55e41`。只读核对原 SQLite 与缓存得到：

- 当前每批最多 5 个测试点，单点用例上限为 6，结构修复允许追加 3 次。
- 失败批次输出 15 条用例，五个点分别关联 1、1、8、2、3 条。
- 点 `point_44275fb59dde489884ce66d428915481` 关联 8 条，超过上限 6。
- 总批次数量未达 30，但仍违反单点限制；此前检索和重排已成功。
- 其他在途请求的 CancelledError 是并发任务在主错误后取消的结果。

旧实现先完成结构修复并缓存，再做单点数量检查；所以本次超限不会进入已有修复流程。
修复涉及 `domain/pipeline.py`、`application/orchestration/pipeline.py` 与模型适配器：

1. 请求携带 `limits.max_cases_per_point`，向模型明确当前上限。
2. 动态 CaseBatch Schema 校验每点数量与批次总上限；超限进入原有有界修复。
3. 只缓存合格结果。新的请求/Schema 指纹绕开旧宽松缓存，不删除历史快照。
4. 最终防御检查仍保留；不提高上限，不截断用例，不放宽引用。

修复耗尽仍会失败；离线通过不能保证真实模型总能遵守契约。

## 验证与未验证项

测试使用虚构文档、临时 SQLite、Fake 向量与 HTTP MockTransport，`RUN_TC_RAG_LIVE=0`，
并显式 `-m 'not live'` 排除真实调用。离线回归包括：

- 超限 8/6 的复现、修复成功、次数耗尽、旧缓存绕过、仅缓存合格结果。
- 同类 ID、跨批引用、边界点、全量目录与章节归属保持。
- 继承字号/粗体、混合标题、正文强调反例及估算开关装配。
- 安全诊断落库后旧严格调用模型可读取、未知字段和值不泄露。
- Excel 追溯/问题分层、进度共享节流、完整材料导出及失配拒绝。

最终验证结果：

| 检查 | 结果 |
|---|---|
| Ruff 全仓库 | All checks passed |
| mypy src | 56 个源码文件无类型问题 |
| 完整离线 pytest | 519 passed，1 skipped，1 deselected；69.61 秒 |
| 跳过项 | 未安装可选 jieba，关键词适配器的对应集成测试未执行 |
| 排除项 | live 标记的真实模型测试明确排除 |
| CLI / 材料工具 | 主帮助、cases 帮助及 prepare_retrieval.py 帮助通过 |
| 图集 | UTF-8 中文、五页 XML、节点唯一与连线引用检查通过 |
| Git 忽略 / 依赖 | workdir 中仅 local.example.yaml 可跟踪；依赖、锁文件和镜像无本轮变动 |

完整日志：`workdir/dev/builds/main-review-20261030/pytest-adopt-final.log`。
这是共享工作区当时的整套结果；其中包含其他会话的测试，不将它们全部算成本轮新增测试。
未实测付费模型、真实需求语义或 Word/XMind/Excel/Draw.io 客户端排版。

## 运行与回退

默认开关保持旧行为；如需要主动启用，可在 local.yaml 设置：

```yaml
parsing:
  infer_docx_headings: true
export:
  point_tree_layout: chapter
```

这会改变解析/目录，应新建运行。只为重试当前失败运行时，先保持原配置，尤其不要改动这些开关或数量上限：

```powershell
# 此命令会产生模型费用，由用户决定何时执行；本轮没有自动执行。
rag cases run_ee9660935a704e74b081554e27d55e41 --config workdir/local.yaml --output-dir workdir/output
```

配置发生变化会明确拒绝复用，应使用原配置或新建完整运行，不能改写历史快照绕过检查。
从已保存的测试点重做用例不重新提炼需求；已成功的生成缓存只有在新契约指纹完全一致时才可复用。
新契约必然失效的旧生成缓存不能承诺复用。

回退应按上述文件的本轮补丁选择性撤回，并先备份当前工作区；不要对有其他会话改动的文件执行整文件还原。
关闭新开关可以回到默认解析/目录；既有运行目录和 SQLite 不移动、不删除，不需要数据库迁移。
新诊断未写进旧调用 JSON，旧读取器仍能解析；新增快照诊断映射可忽略。回退数量修复会恢复原来的超限失败风险。
