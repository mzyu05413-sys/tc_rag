# tc_rag

面向测试设计的 RAG 框架：需求文档 → 图文解析 → 功能与规则提炼 → 向量索引与检索 →
检索增强规划测试点 → Skill 与检索增强生成用例 → 输出 XMind、Excel 与质量报告。

只保留这一条 RAG 流程。架构、模块分工与审核点见
[RAG 主线说明](docs/learn/P2/README.md) 和 [完整运行说明](docs/full_run_walkthrough.md)。

## 快速开始

```powershell
uv sync --extra dev
```

首次使用时需要准备 `workdir` 目录，将仓库中的
[`workdir/local.example.yaml`](workdir/local.example.yaml) 复制为 `workdir/local.yaml`，
再填写模型配置。示例文件不会自动加载；已有 `local.yaml` 时不要覆盖。

macOS / Linux：

```bash
mkdir -p workdir
cp -n workdir/local.example.yaml workdir/local.yaml
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force workdir | Out-Null
if (-not (Test-Path workdir/local.yaml)) {
    Copy-Item workdir/local.example.yaml workdir/local.yaml
}
```

编辑 `workdir/local.yaml`，按实际服务填写以下参数：

| 配置 | 必填内容与作用 |
| --- | --- |
| `models.llm` | 必需：`base_url`、`model`、`api_key` 或 `api_key_env`；`concurrency` 控制提炼、规划和用例生成的批次并发，示例为 3 |
| `models.embedding` | 必需：地址、模型、密钥；`batch_size` 为单批文本数，示例 16；`concurrency` 为向量批次并发，示例 2 |
| `retrieval.embedding_dimension` | 必须与 embedding 模型实际输出维度一致，示例 1536 不是所有模型的通用值 |
| `models.vision` | 可选：启用时配置地址、模型、密钥、`supports_images: true` 及并发数，同时设 `parsing.vision_enabled: true` |
| `models.rerank` | 可选：启用时填写模型、密钥和完整重排 REST 地址；当前随上层批次并发，单独设置其 `concurrency` 不会产生独立并发限制 |

生成模型的 `response_format` 按服务能力选择 `json_schema` 或 `json_object`；
`thinking_mode` 仅在服务支持时设置。并发数应按服务商实际限额调整，遇到限流先降低并发；
视觉还可以用 `runtime.vision_min_interval_seconds` 控制请求启动间隔。
密钥可以填在被 Git 忽略的 `local.yaml` 中，非空值优先，否则读取 `api_key_env` 指定的环境变量。
不要将真实密钥写入 `local.example.yaml`。

配置完成后，在项目根目录执行：

```powershell
uv run rag doctor
uv run rag
```

`doctor` 只做离线配置检查，不证明服务连通；`doctor --live` 会调用真实模型并可能产生费用。
配置选择顺序：显式 `--config` → `workdir/local.yaml` → `config/defaults.yaml` → 内置默认；
选中一份 YAML 后再叠加环境变量和 CLI 覆盖，不会自动合并所有 YAML 文件。

`rag` 会提示输入需求文档路径（PDF/DOCX），回车后自动使用内置生成目标：

> 根据需求文档生成功能测试点和测试用例，保留原文依据

中间不需要审核或二次执行。完成后输出：

- XMind 测试点目录
- 全量测试用例 Excel
- JSON / Markdown 质量报告

产物默认在 `workdir/output/<运行ID>/`。

> 虚拟环境未激活时使用 `uv run rag`；`tc-rag` 与 `rag` 等价。
> 非交互环境（管道、CI）不带子命令时只显示帮助，不会挂起等待输入。

## 输出文件命名

Excel 和 XMind 都带上传文档名后缀，避免同名：

| 产物       | 文件名                                           |
| ---------- | ------------------------------------------------ |
| XMind      | `testpoints_<需求文件名>.xmind`                |
| Excel      | `testcases_<需求文件名>.xlsx`                  |
| 规范 JSON  | `testcases.json`                               |
| 评审报告   | `report.md`                                    |
| 运行清单   | `manifest.json`                                |
| 质量与指标 | `reports/quality.md`、`reports/metrics.json` |

示例：`需求文档.pdf` → `testpoints_需求文档.xmind`、`testcases_需求文档.xlsx`。

文件名会去掉 `< > : " / \ | ? *` 等非法字符，最长 60 字符；无法提取时使用 `requirement`。
同一次运行重试或重新生成会创建 `batch_<id>/` 子目录，保证 XMind 与 Excel 来自同一批事实。

## 配置

日常只需要改 `workdir/local.yaml`：

- 各模型可直接在 `api_key` 填写密钥，也可留空并配合 `api_key_env`。
- 密钥字段不会进入运行快照和导出；`workdir/` 已被 Git 忽略。
- 正式运行必须配置 `models.llm` 与 `models.embedding`；
  `retrieval.embedding_dimension` 必须等于向量模型实际维度。
- `vision`、`rerank` 按需启用。

文件选择顺序：显式 `--config` → `workdir/local.yaml` → `config/defaults.yaml` → 内置默认。
选定文件后，再按默认值 → 文件 → 环境变量 → CLI 覆盖加载。

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

- `doctor`：检查配置与凭据是否存在，不证明服务真实连通。
- `estimate`：本地解析分块，粗略估算提炼调用量，不含图片、重试与用例生成。

### 查看运行与重新导出

```powershell
rag status <run_id>
rag inspect <run_id> --stage rules
rag inspect <run_id> --stage cases
rag inspect <run_id> --stage failure
rag export <run_id> --format xlsx
```

`export` 支持 `xlsx` / `json` / `markdown`，从 SQLite 重建派生文件，
不重新生成用例，也不修改原运行状态。

### 重试用例生成或恢复运行

```powershell
rag cases <run_id> -i "与原运行一致的生成指令"
rag run "D:\需求资料\需求文档.pdf" -i "与原运行一致的生成指令" --resume <run_id> --from-stage extract
```

- `cases`：复用已保存测试点，只重做用例阶段，配置必须与原运行一致。
- `--resume`：从失败或中断处继续；`--from-stage` 支持 `extract` / `plan_points`。
- 恢复运行要求文档、指令和配置与原运行一致。

### 真实连通检查与评估

```powershell
rag doctor --live
rag evaluate <run_id> --gold workdir/dev/demo/gold.json
```

- `doctor --live` 会调用生成与向量服务，最多 8 次请求，使用虚构需求。
- `evaluate` 不调用模型，但需要符合项目评估契约的人工标注 JSON。

## 常用选项

| 选项                     | 用途                                                 |
| ------------------------ | ---------------------------------------------------- |
| `--config <file>`      | 指定配置文件                                         |
| `--output-dir <dir>`   | 输出根目录，适用 `start/run/cases/export`          |
| `--preview`            | 只预览计划，不调用模型                               |
| `--quiet`              | 隐藏执行进度，保留结果与错误                         |
| `--skill-file <md>`    | 追加用例表达规范（UTF-8、≤256 KiB）                 |
| `--resume <run_id>`    | 恢复已有运行                                         |
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

默认加载 [用例生成 Skill](prompts/case_skill/v1.md)；
需要附加自定义规范时使用 `--skill-file`，或配置 `generation.skill_file`。

## 安装与验证

项目已在 `pyproject.toml` 中将阿里云 PyPI 镜像配置为 uv 默认源，
在项目根目录执行以下命令即可，无需另加镜像参数。
`uv.lock` 已记录阿里云镜像来源；未来修改镜像配置后执行普通 `uv sync` 更新锁文件，
不要用 `--frozen` 跳过更新。镜像配置仅影响 uv，不影响 pip。
Intel Mac（macOS x86_64）使用 PyMuPDF 1.x，避免 2.x 缺少该平台 wheel 及其新增依赖的兼容问题。

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
- [架构图（draw.io）](docs/reference/tc_rag_architecture_atlas.drawio)
- [架构图核对记录](docs/reference/architecture_atlas_review.md)
- [开发指南](docs/design/dev_guide.md)
- [按模块学习](docs/learn/README.md)
- [完整运行说明](docs/full_run_walkthrough.md)
