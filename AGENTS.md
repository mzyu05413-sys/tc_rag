# tc_rag 项目协作规则

本文件供 Codex 在本仓库工作时读取。使用中文沟通，文件统一使用 UTF-8。

## 项目与设计依据

- 项目目标：PDF/DOCX 需求文档 → 可追溯证据 → 功能与规则 → 测试点 → 测试用例及覆盖报告。
- 当前输入只保留需求文档；已移除代码仓库读取、源码索引及代码上下文支路，不恢复为平行输入路线。
- 设计依据：`docs/design/design_spec.md`；分阶段路线：`docs/design/dev_guide.md`。
- 设计与指南描述当前约束和后续规划，不自动授权执行；以用户当前请求确定任务范围。
- 当前仅保留 RAG 连续生成流程；P0/P1 是基础模块，P2 提供检索增强。真实业务质量与客户端仍待验收。
  当前文档入口：`docs/README.md`；学习导航：`docs/learn/README.md`。
  P2.1～P2.9 已提供实现与离线验证，RAG 参与规划和生成；真实业务效果仍待验收。
  当前架构、使用及审核入口：`docs/learn/P2/README.md`；生成与 embedding 必需，图片/重排等扩展按配置启用；禁止恢复无检索生成分支。
  P0 领域契约补充见 `docs/reference/contracts.md`。
  后续进度以实际代码、测试和交付记录为准，不将目标设计描述成已实现功能。

## 工作范围与沟通

- 用户原始要求中的图文分离、图片转文字、批量并发向量、FAISS、相似度、rerank、计时均须保留；扩展声明不能冒充已实现。
- 按用户指定阶段逐步完成；用户要求“下一步”时，沿开发指南继续。
- 开始前阅读相关设计章节和现有实现；不顺手实现后续阶段功能。
- 已授权的开发和可逆修改直接推进，不为常规实现选择反复索要确认。
- 涉及业务含义或设计契约的不确定项，明确记录假设；关键歧义向用户确认。
- 完成后说明：改动、验证结果、未验证项、需要用户审核的具体点和下一步。
- 未经用户要求，不自动提交、打标签、发布或调用付费真实模型。

## 架构与数据约束

依赖方向：`interfaces → application → domain`。

`infrastructure` 实现 `application.ports`；具体实现由 `bootstrap.py` 装配。

`domain` 只放领域对象和纯逻辑，不导入供应商 SDK、数据库或执行文件 I/O。

模型、解析、存储、向量检索、rerank、vision 等外部能力通过 ports 访问；
在相应阶段提供 Fake 或模拟适配器，默认测试不访问网络。

SQLite 是结构化业务事实的唯一来源；JSON、Markdown、Excel、向量索引属于派生产物。

保留 Block、Chunk 字符区间和证据来源；vision 与原文证据明确区分；origin=ocr 仅用于历史证据兼容，不代表当前识别能力。

不编造需求中的错误码、提示文案、角色、数值、单位或精度；缺失信息进入待确认项。

自动校验通过不等于人工批准；结构校验不等于证明语义有依据。
用户已移除中间人工审核：一次命令连续完成规划和用例生成，联合输出测试点 XMind 与 Excel。
XMind 展示全部测试点（按状态 → 业务维度 → 功能分层，含待澄清），Excel 展示已生成用例；
XMind 不回读为生成输入；不恢复 review 命令或伪造批准记录。
用例 Skill 默认参与生成，可追加 Markdown 规范，不能覆盖原文事实、引用约束和输出结构。

提示词放在 `prompts/<stage>/<version>.md`，不内嵌在业务代码中；变更时记录版本。

## 配置与依赖

- 配置顺序：默认值 → YAML 文件 → 环境变量 → CLI 覆盖，最终通过 Pydantic 校验。
- 用户允许在被 Git 忽略的本地配置 `workdir/local.yaml` 使用 `models.<role>.api_key`。
  非空配置密钥优先，否则回退 `api_key_env`；SecretStr 字段禁止参与序列化和运行快照。
  不把真实密钥写进代码、公共 YAML、日志、测试输出或导出产物；不读取后回显用户密钥。
- `pyproject.toml` 是依赖声明主来源；变更时同步维护带用途注释的
  `requirements/requirements.txt`、`requirements/requirements-dev.txt` 和 `requirements/requirements-optional.txt`。
- 引入依赖时说明用途，优先复用现有库；可选适配器放 extras，不进入核心依赖。
- requirements 清单不等于版本锁文件；只有实际生成锁文件后才能声明版本已锁定。
- 不提交 `.env`、运行数据库、虚拟环境、缓存或未经用户授权纳入仓库的真实需求资料。

## 目录维护

- 日常配置只维护 `workdir/local.yaml`，旧配置备份放 `workdir/dev/backups/`，不得默认加载。
- `workdir/data.sqlite` 及其 WAL/SHM、`assets/`、`indexes/`、`output/`、`diagnostics/`
  是运行数据；已有文件路径可能被资产、索引、审核和配置快照引用，不随目录整理移动。
- 开发资料集中放 `workdir/dev/`：测试临时文件 `tests/`，构建/安装验证 `builds/`，
  开发工具依赖 `tools/`，检查缓存 `cache/`，性能结果 `benchmarks/`，私有演示 `demo/`。
  历史维护脚本放 `scripts/history/`，不用时不在根目录留零散脚本。
- pytest 默认临时目录为 `workdir/dev/tests/current`，会被 pytest 重建；并行运行或保留记录时，
  用 `--basetemp=workdir/dev/tests/独立任务名` 覆盖，禁止再创建 `workdir/pytest-*`。
- 本机临时工具若通过 PYTHONPATH 导入，使用 `workdir/dev/tools/quality-tools` 和
  `workdir/dev/tools/optional-tools`；它们不是正式运行依赖，不复制到源码目录。
- 详细职责和清理边界见 `docs/reference/directory_structure.md`。

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
