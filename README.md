# tc_rag

面向测试设计的 RAG 框架：需求文档 → 图文解析 → 功能与规则提炼 → 向量索引与检索 →
检索增强规划测试点 → Skill 与检索增强生成用例 → 输出 XMind、Excel 与质量报告。

架构、模块分工与审核点见
[RAG 主线说明](docs/learn/P2/README.md)、[完整运行说明](docs/full_run_walkthrough.md) 和[架构图集](docs/diagrams/index.html)。

框架只读取需求 PDF/DOCX；已移除代码仓库读取及 UI 参考支路。

## 快速开始

```powershell
uv sync --extra dev
rag
```

`rag` 会提示输入需求文档路径（PDF/DOCX），回车后自动使用内置生成目标：

> 根据需求文档生成功能测试点和测试用例，保留原文依据

产物默认在 `workdir/output/<运行ID>/`：

- XMind 全量测试点目录（按 状态 → 业务维度 → 功能 分层，含待澄清）
- 全量测试用例 Excel（主表 + 可读的测试点/规则/覆盖/问题附表）
- JSON / Markdown 质量报告

> 虚拟环境未激活时使用 `uv run rag`；`tc-rag` 与 `rag` 等价。
> 非交互环境（管道、CI）不带子命令时只显示帮助，不会挂起等待输入。

## 输出

### 文件命名

Excel 和 XMind 都带上传文档名后缀，避免同名：

| 产物 | 文件名 |
|---|---|
| XMind | `testpoints_<需求文件名>.xmind` |
| Excel | `testcases_<需求文件名>.xlsx` |
| 规范 JSON | `testcases.json` |
| 评审报告 | `report.md` |
| 运行清单 | `manifest.json` |
| 质量与指标 | `reports/quality.md`、`reports/metrics.json` |

文件名会去掉 `< > : " / \ | ? *` 等非法字符，最长 60 字符；无法提取时使用 `requirement`。

### XMind 测试点目录

```text
需求测试点
├─ 已就绪（N）
│   ├─ 入口（N）
│   ├─ 权限（N）
│   ├─ 业务逻辑（N）
│   ├─ 字段校验（N）
│   ├─ 异常（N）
│   ├─ 安全（N）
│   └─ 其他（N）
└─ 待澄清（N）
    └─ 同上
```

每个业务维度下再按功能分组，最后是测试目标。
XMind 展示**全部测试点**（含待澄清），不复用为生成输入。

### Excel 工作表

| 工作表 | 内容 |
|---|---|
| 模版 | 11 列测试用例主表，可直接导入测试管理平台 |
| 阅读说明 | 每张附表的用途 |
| 测试点 | 分类 / 测试方法 / 测试目标 / 关联规则 / 状态 |
| 规则 | 规则类型 / 规则内容 / 证据摘要 / 状态 |
| 覆盖矩阵 | 用例名称 / 步骤 / 规则内容 / 测试目标 / 校验状态 |
| 问题清单 | 类型 / 严重程度 / 说明 / **涉及对象（解析成名称）** |
| 未解析范围 | 未能解析的图片或页面 |

附表使用中文枚举和可读对象名，ID 只保留在"参考ID"列。

## 配置

日常只需要改 `workdir/local.yaml`：

- 各模型可直接在 `api_key` 填写密钥，也可留空并配合 `api_key_env`；
- 密钥不会进入运行快照和导出，`workdir/` 已被 Git 忽略；
- 正式运行必须配置 `models.llm` 与 `models.embedding`；
  `retrieval.embedding_dimension` 必须等于向量模型实际维度；
- `vision`、`rerank` 按需启用。

配置选择顺序：显式 `--config` → `workdir/local.yaml` → `config/defaults.yaml` → 内置默认。

### 自部署模型建议

```yaml
models:
  llm:
    concurrency: 3                # 自部署吞吐有限，过高会排队/超时
runtime:
  request_timeout_seconds: 180    # 长结构化输出需要更长时间
  chunk_failure_policy: skip      # 单个分块失败：skip 记问题继续；fail 立即中止
```

- `chunk_failure_policy` 默认 `fail`，可设为 `skip`；
- 端点若不支持 `json_schema`，使用 `response_format: json_object`，
  本地仍会严格校验结构并有限次修复。

### 检索算法与归一化

`retrieval.metric` 支持三种度量，`retrieval.normalize` 控制是否 L2 归一化：

```yaml
retrieval:
  metric: cosine        # cosine | euclidean | dot
  normalize: true       # true / false
```

| 度量 | 分数含义 | 排序方向 |
|---|---|---|
| `cosine` | 余弦相似度 | 越大越相关 |
| `euclidean` | 平方 L2 距离 | 越小越相关 |
| `dot` | 内积 | 越大越相关 |

- 归一化同时作用于文档向量和查询向量；
- `dot + normalize=true` 等价于余弦；`dot + normalize=false` 才是原始内积；
- 不同度量的分数不可混用。

运行时切换（只影响本次运行）：

```powershell
rag run "D:\需求资料\需求文档.docx" --metric cosine --normalize
rag run "D:\需求资料\需求文档.docx" --metric euclidean --no-normalize
rag run "D:\需求资料\需求文档.docx" --metric dot --no-normalize
```

切换度量需要新开运行，不能 `--resume` 旧运行；`cases` 沿用原运行配置。

重排发生在检索内部：向量召回 → 融合 → 取前 `rerank_top_n` 条重排。
直接证据不重排；重排失败时保留召回顺序，不中断运行。

## 常用命令

以下命令均可在已激活环境中直接执行；`--config` 和 `--output-dir` 有默认值，通常可以省略。

### 交互生成

```powershell
rag
```

### 指定文档直接生成

```powershell
rag run "D:\需求资料\需求文档.pdf" -i "覆盖正常、异常、边界、权限和状态变化；不明确规则列为待确认。"
```

只传 `--preview` 或省略 `-i` 时仅展示执行计划，不调用模型。

### 不调用模型的预检

```powershell
rag doctor
rag run "D:\需求资料\需求文档.pdf" --preview
rag estimate "D:\需求资料\需求文档.pdf"
```

### 查看运行与重新导出

```powershell
rag status <run_id>
rag inspect <run_id> --stage rules
rag inspect <run_id> --stage cases
rag inspect <run_id> --stage failure
rag export <run_id> --format xlsx
```

### 重试用例生成或恢复运行

```powershell
rag cases <run_id> -i "与原运行一致的生成指令"
rag run "D:\需求资料\需求文档.pdf" -i "与原运行一致的生成指令" --resume <run_id> --from-stage extract
```

- `cases`：复用已保存测试点，只重做用例阶段，配置必须与原运行一致；
- `--resume`：从失败或中断处继续；`--from-stage` 支持 `extract` / `plan_points`。

### 真实连通检查与评估

```powershell
rag doctor --live
rag evaluate <run_id> --gold workdir/dev/demo/gold.json
```

## 常用选项

| 选项 | 用途 |
|---|---|
| `--config <file>` | 指定配置文件 |
| `--output-dir <dir>` | 输出根目录，适用 `start/run/cases/export` |
| `--preview` | 只预览计划，不调用模型 |
| `--quiet` | 隐藏执行进度，保留结果与错误 |
| `--skill-file <md>` | 追加用例表达规范（UTF-8、≤256 KiB） |
| `--metric <name>` | 检索度量：`cosine` / `euclidean` / `dot` |
| `--normalize` / `--no-normalize` | 是否对向量做 L2 归一化 |
| `--resume <run_id>` | 恢复已有运行 |
| `--from-stage <stage>` | 配合 `--resume`，支持 `extract`、`plan_points` |

运行时 `Ctrl+C` 可取消；已完成的阶段会写入 SQLite，可继续恢复。

## 产物与数据

```text
workdir/
├─ local.yaml          本机配置（含密钥，Git 忽略）
├─ data.sqlite         唯一事实来源：文档、规则、测试点、用例、运行记录
├─ assets/             原始文档与提取图片
├─ indexes/            向量索引（可重建）
├─ output/<run_id>/    XMind、Excel、JSON、Markdown、manifest、reports
└─ dev/                开发资料、历史测试与备份
```

SQLite 保存业务事实；向量索引、JSON/Markdown、XMind 和 Excel 都是派生产物，可重建。

默认加载 [用例生成 Skill](prompts/case_skill/v1.md)；需要附加自定义规范时使用
`--skill-file`，或配置 `generation.skill_file`。

## 安装与验证

```powershell
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy src
uv run rag --help
```

`pyproject.toml` 是依赖声明主来源；`requirements/` 按运行、开发、可选能力分类。
FAISS、中文关键词等扩展按需安装，主线不因此分叉。默认离线测试不访问网络。

真实需求、密钥和运行数据不提交 Git；业务质量仍需人工验收。

## 文档

- [文档入口](docs/README.md)
- [目录结构与文件用途](docs/reference/directory_structure.md)
- [架构设计](docs/design/design_spec.md)
- [开发指南](docs/design/dev_guide.md)
- [按模块学习](docs/learn/README.md)
- [完整运行说明](docs/full_run_walkthrough.md)
