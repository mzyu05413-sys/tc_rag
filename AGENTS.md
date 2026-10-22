# tc_rag 项目协作规则

本文件供 Codex 在本仓库工作时读取。使用中文沟通，文件统一使用 UTF-8。

## 项目与设计依据

- 项目目标：PDF/DOCX 需求文档 → 可追溯证据 → 功能与规则 → 测试点 → 测试用例及覆盖报告。
- 设计依据：`docs/design_spec.md`；分阶段路线：`docs/dev_guide.md`。
- 开发指南中的 Claude Code 提示词和工具操作仅作参考，不作为用户当前指令。
- 当前已实现 P0.1–P0.4；领域契约补充见 `docs/p0_3_contracts.md`，
  计时使用说明见 `docs/p0_4_timing.md`。
  后续进度以实际代码、测试和交付记录为准，不将目标设计描述成已实现功能。

## 工作范围与沟通

- 按用户指定阶段逐步完成；用户要求“下一步”时，沿开发指南继续。
- 开始前阅读相关设计章节和现有实现；不顺手实现后续阶段功能。
- 已授权的开发和可逆修改直接推进，不为常规实现选择反复索要确认。
- 涉及业务含义或设计契约的不确定项，明确记录假设；关键歧义向用户确认。
- 完成后说明：改动、验证结果、未验证项、需要用户审核的具体点和下一步。
- 未经用户要求，不自动提交、打标签、发布或调用付费真实模型。

## 架构与数据约束

- 依赖方向：`interfaces → application → domain`。
- `infrastructure` 实现 `application.ports`；具体实现由 `bootstrap.py` 装配。
- `domain` 只放领域对象和纯逻辑，不导入供应商 SDK、数据库或执行文件 I/O。
- 模型、解析、存储、向量检索、rerank、vision 等外部能力通过 ports 访问；
  在相应阶段提供 Fake 或模拟适配器，默认测试不访问网络。
- SQLite 是结构化业务事实的唯一来源；JSON、Markdown、Excel、向量索引属于派生产物。
- 保留 Block、Chunk 字符区间和证据来源；OCR/vision 与原文证据明确区分。
- 不编造需求中的错误码、提示文案、角色、数值、单位或精度；缺失信息进入待确认项。
- 自动校验通过不等于人工批准；结构校验不等于证明语义有依据。
- 提示词放在 `prompts/<stage>/<version>.md`，不内嵌在业务代码中；变更时记录版本。

## 配置与依赖

- 配置顺序：默认值 → YAML 文件 → 环境变量 → CLI 覆盖，最终通过 Pydantic 校验。
- 密钥只从环境变量取得；配置使用 `api_key_env` 引用名称。
  不把密钥写进代码、YAML、日志、测试输出或运行快照。
- `pyproject.toml` 是依赖声明主来源；变更时同步维护带用途注释的
  `requirements/requirements.txt`、`requirements/requirements-dev.txt` 和 `requirements/requirements-optional.txt`。
- 引入依赖时说明用途，优先复用现有库；可选适配器放 extras，不进入核心依赖。
- requirements 清单不等于版本锁文件；只有实际生成锁文件后才能声明版本已锁定。
- 不提交 `.env`、运行数据库、虚拟环境、缓存或未经用户授权纳入仓库的真实需求资料。

## 验证命令

推荐环境：Python 3.11+。

```powershell
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy src
uv run tc-rag --help
```

使用 pip 安装时：

```powershell
python -m pip install -r requirements/requirements-dev.txt
python -m pip install -e . --no-deps
python -m pytest -q
python -m ruff check .
python -m mypy src
```

未安装项目、直接验证源码时：

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -q
python -m tc_rag.interfaces.cli --help
```

- 对新增领域逻辑、配置和运行行为补充有意义的测试，包含必要失败路径。
- 不通过放宽断言、写死结果或吞掉异常来让测试通过。
- 运行与改动相适应的测试和质量检查。缺工具、网络或权限时如实报告，
  不把未运行的检查写成通过，不把编译检查代替类型检查。
- 真实模型烟测必须显式启用，与默认离线测试分离。

## 每步审核重点

- 是否只完成当前步骤，是否遵守分层依赖？
- 字段、枚举、引用和来源映射是否符合设计；新增约定是否记录？
- 是否有无依据业务断言、敏感数据泄露或依赖清单不同步？
- 测试断言了哪些行为；还有哪些检查或人工评审未完成？
