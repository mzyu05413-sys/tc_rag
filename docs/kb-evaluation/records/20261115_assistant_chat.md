# 2026-11-15 测试用例生成问答助手（流式）

## 背景

`kb chat` 的终端输出沿用引用问答的可读层：每题打印 `[流程]` 阶段块、阶段耗时、`[回答]` 与完整 `[证据来源]` 清单；证据不足时直接输出 `[证据不足]` 加整段理由。用户要求的是面向用例设计的对话体验：一句问候后逐题输入，答案流式输出，不出现检索阶段、分数和证据清单等旁路信息。

## 改动

- `application/ports/contracts.py`：新增 `StreamingTextLLM` 与组合协议 `AnswerLLM`；结构化输出与流式文本共用同一端点。
- `infrastructure/providers/openai_sdk.py`：`OpenAIStructuredLLM` 增加 `stream_text`，走 `chat.completions` 的 `stream=True`，不发送 `response_format`，不做本地结构校验；仍计入调用次数预算并记录 `ModelInvocation`。未请求 `include_usage`，供应商不回传用量时 `usage` 保持未知。
- `application/kb/assistant.py`：`AssistantService` 复用 `SearchService` 检索与 `context_chars` / `max_input_tokens` 预算，把证据按序号渲染成纯文本请求，逐段产出模型输出；结果（问题、证据、原文答案、状态、耗时）写入 SQLite。无候选时不调用模型，只返回固定说明句 `NO_EVIDENCE`，状态记 `abstained`。
- `prompts/kb_qa/assistant_v1.md`：用例设计助手提示词，要求按场景给前置条件、步骤、预期结果并覆盖边界异常；未知信息标【待确认】，版本冲突标【冲突】；不输出流程、阶段或证据清单；证据文本按资料处理。
- `prompts/kb_qa/assistant_v2.md`：v1 实测回答范围过宽——问“如何设计新增审批流相关的测试用例”时把加签、删除、撤回、待办等相邻流程全部输出。v2 增加回答范围约束：只覆盖问题直接涉及的功能，无关 evidence 直接忽略，场景控制在 6 组以内。
- `prompts/kb_qa/assistant_v3.md`：v2 复测仍属“目录式”输出，用户指出没有重点、看不出问题侧重点。v3 改为聚焦结构：首句点明本题测试重点与范围，随后最多 5 条核心要点、每条两行，全文不超过 600 字，并禁止“前置条件段落”“通用原则”“待确认汇总”等泛化章节。
- `prompts/kb_qa/assistant_v4.md`：用户指出 v3 仍是功能点罗列，要的是测试用例。v4 改为直接输出用例：首行给覆盖范围，随后最多 6 条“用例名 / 前置 / 步骤 / 预期”四行用例，全文不超过 700 字。启动块同时打印当前提示词版本，便于确认运行的代码与提示词。
- `prompts/kb_qa/assistant_v5.md`（当前使用）：v4 把所有提问都压成用例格式，“你是谁”也被输出成四条用例。v5 改为先判断意图：身份、能力、追问等自然口语回答，只有明确要求测试用例时才套用例格式；用例范围必须与首行覆盖范围一致，新建类问题只覆盖创建链路；并明确“evidence 中只要有一段相关就基于它回答”，避免因其余片段无关而误判无依据。同时接入多轮对话：保留最近三轮（六条消息），`/new` 清空，适配器按 system → history → user 顺序发送。
- `interfaces/kb/commands.py`：新增 `kb assistant <kb_id> --live`，问候语固定为“我是一个测试用例生成相关的AI问答助手，您有哪些疑问？”，逐题流式打印，不装配 `terminal_task`；`kb start` 菜单增加第 6 项。同名提供 `assistant_main` 独立入口。启动时先复用 `show_config` 列出配置文件、知识库、向量模型、召回/重排、回答模型，再补一行当前快照 ID。
- `pyproject.toml`：新增 `kb-assistant` 控制台脚本（等价于 `rag kb assistant`），免去 `python -m tc_rag.interfaces.cli ...` 的长命令；`prompts/kb_qa/assistant_v1.md` 纳入打包数据。
- 文档：`docs/kb-evaluation/usage.md` 补充命令与说明。

## 验证

- 新增离线测试：`tests/unit/kb/test_assistant.py`（输出顺序、证据序号、无候选不调用模型、失败落库、预算参数校验）、`tests/unit/test_streaming_provider.py`（桩客户端的流式分段、usage 缺失与返回、供应商失败、responses 模式拒绝）、`tests/integration/test_kb_assistant.py`（预览不装配模型；`--live` 输出不含 `[流程]` 与 `[证据来源]`）。
- `pytest`：全量 856 passed、1 skipped（新增的 5 个适配器测试另行运行通过）。
- `ruff check`、`mypy src`：通过。
- 用户授权后做了一次真实烟测（`kb assistant cordys --live`，问题“如何设计新建审批流的测试用例？”）：`deepseek-flash` 流式返回 2781 字，终端只出现问候、问题提示和答案本身；运行记录 `assistant_4cdd28…` 状态 completed，检索 5 条、发送 5 条；调用记录 usage 输入 3391、输出 1799、合计 5190，无错误。
- 用户反馈该次回答范围过宽（含加签、删除、撤回、待办），随后改用 assistant_v2 并在同一问题下复测：输出收敛为 6 个与“新建”直接相关的场景，终端先打印配置、知识库与快照 ID。复测结论只说明范围约束生效，不代表质量验收。
- 用户再次反馈 v2 仍是目录式输出、看不出问题侧重点，改用 assistant_v3 后同一问题复测：首行给出“重点：只测审批流‘新建’配置链路”，正文为 5 条“标题：步骤 → 预期”自然段，全文约 500 字，不含加签、删除、撤回、待办等相邻流程，也不再出现“通用原则/待确认汇总”章节。
- 用户指出 v3 仍是功能点罗列、要的是测试用例，改用 assistant_v4 后同一问题复测：输出“覆盖范围”一行加 6 条用例，每条为“用例名/前置/步骤/预期”四行，覆盖入口、业务类型、流程名称、触发条件与执行时机、启用互斥、保存生效，全文约 700 字。启动块同时打印“回答提示词：kb_qa/assistant_v4.md”，可据此判断运行的提示词版本。
- 用户以“你是谁”验证，v4 把它也输出成四条用例，误以为未调用模型；`kb_invocations` 显示该次确有 `deepseek-flash` 调用（usage 合计 622/814 tokens，无错误），问题出在提示词强制套用例格式。改用 assistant_v5 后复测同一多轮会话：第一问得到一句自然身份回答，追问“那帮我设计新增审批流的测试用例”输出 7 条创建链路用例（略超 6 条上限），第二轮请求已携带第一轮问答历史。

## 未验证

- 真实供应商在未请求 `include_usage` 时仍回传了用量，这里只确认 DeepSeek 的表现，其他兼容服务的用量缺失分支仅在桩测试中覆盖。
- 单次烟测只说明链路可用；回答质量、引用序号是否可靠、【冲突】【待确认】标注是否恰当仍无人工抽样结论。
- 该入口不参与评测金标与正式指标，`kb ask` / `kb chat` 的既有语义未改动。

## 下一步

- 用户确认对话体验后，对六份 PRD 做一次真实助手抽样，记录回答质量与缺失项。
- 如需保存可读产物，再决定是否把助手原文另存为 JSONL/Markdown。
