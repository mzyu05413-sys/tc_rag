# 目录结构与文件用途

[文档入口](../README.md) · [RAG 主线与启动命令](../learn/P2/README.md)

日常使用只需修改 `workdir/local.yaml`，生成完成后查看 `workdir/output/`。
其余业务数据由程序维护；开发测试资料统一进入 `workdir/dev/`。
框架只读取需求 PDF/DOCX；源码扫描、代码索引及其配置支路已移除。运行目录中既有历史资产不随本次收敛移动或删除。

## 项目目录

```text
RAG/
├─ README.md                  项目入口及启动说明
├─ AGENTS.md                  Codex 开发协作约定
├─ config/                    公共默认配置和示例，不放真实密钥
├─ requirements/              运行、开发、可选依赖及用途
├─ prompts/                   各阶段的版本化提示词
├─ src/tc_rag/                框架代码
├─ tests/                     自动化测试代码
├─ docs/                      架构、开发指南、学习和参考资料
├─ scripts/                   可复用的开发辅助脚本
├─ eval/                      评估入口与离线检索材料准备，见该目录 README
├─ workdir/                   本机配置、业务/开发数据；仅无密钥 local.example.yaml 可进 Git
├─ .venv/                     Python 虚拟环境
├─ pyproject.toml             项目、依赖和检查工具配置
└─ uv.lock                    uv 依赖锁文件
```

`.git/` 是本地 Git 元数据；`.uv-cache/`、`build/` 等可能由安装或构建工具生成，
不属于业务流程。pytest、Ruff 和 mypy 的默认缓存已经集中到 `workdir/dev/cache/`。
本机原有根目录 `.pytest_cache/` 因系统访问权限限制保留原位，后续默认测试使用新缓存目录。

## workdir：日常运行与开发资料

下面展示的是目录约定；`indexes/`、`output/` 等会在执行到对应阶段后生成。

```text
workdir/
├─ local.yaml                 唯一默认本地配置，模型与密钥填这里
├─ local.example.yaml         可跟踪的无密钥示例，不自动加载，不覆盖现有 local.yaml
├─ data.sqlite                文档、证据、规则、审核、用例和运行记录
├─ data.sqlite-wal            SQLite 日志文件，运行时可能出现
├─ data.sqlite-shm            SQLite 共享内存文件，运行时可能出现
├─ assets/                    原始文档副本及提取图片
├─ indexes/                   NumPy/FAISS 向量索引
├─ output/                    生成的 XMind、Excel、JSON、Markdown 和报告
├─ diagnostics/               CLI 诊断及排错材料
└─ dev/                       开发资料，日常生成无需操作
   ├─ tests/
   │  ├─ current/             pytest 默认临时目录，每次运行可重建
   │  └─ history/             整理前积累的测试目录
   ├─ builds/history/         历史 wheel、安装副本及安装验证数据库
   ├─ tools/
   │  ├─ quality-tools/       本机临时检查工具
   │  └─ optional-tools/      本机临时可选依赖
   ├─ cache/                  pytest、Ruff、mypy 等开发缓存
   ├─ benchmarks/             相似度等性能测量结果
   ├─ demo/                   私有演示、人工标注和评估辅助资料
   ├─ backups/                旧配置备份，不自动加载
   ├─ scripts/history/        历史文档整理和调试脚本
   └─ maintenance/            本次移动清单，用于核对原位置和新位置
```

`dev/` 可能包含私有资料、旧密钥或测试数据，整个目录继续由 Git 忽略。
历史安装副本是当时的代码快照，不能作为当前程序启动入口。

## 配置只需要维护哪份

| 文件                               | 用途                                  |
| ---------------------------------- | ------------------------------------- |
| `workdir/local.yaml`             | 实际使用的模型、密钥、检索及导出参数  |
| `config/defaults.yaml`           | 公共默认配置模板                      |
| `config/example.yaml`            | 公共示例，不会被自动选中              |
| `workdir/local.example.yaml`     | 首次复制到 local.yaml 的无密钥模板，不自动加载 |
| `workdir/dev/backups/` 中的 YAML | 历史备份，不参与正常启动              |
| 诊断或测试目录中的配置             | 仅供对应诊断/测试使用，不会被自动扫描 |

CLI 选择顺序为显式 `--config` → `workdir/local.yaml` → `config/defaults.yaml` → 内置默认。
选择一份文件后再叠加环境变量与命令覆盖，并非依次加载所有 YAML。

## 代码按职责看

```text
src/tc_rag/
├─ interfaces/                CLI 命令、交互与进度
├─ application/
│  ├─ orchestration/          规划与生成连续执行的流程编排
│  ├─ services/               解析后处理、分块、设计、评估与质检
│  └─ ports/                  外部能力的接口约定
├─ domain/                    领域对象与纯业务规则
├─ infrastructure/
│  ├─ parsing/                PDF、DOCX 解析
│  ├─ providers/              生成、向量、重排和视觉模型调用
│  ├─ retrieval/              索引、相似度、召回与上下文
│  ├─ storage/                SQLite、文件及数据库迁移
│  ├─ exporters/              XMind、Excel 和报告导出
│  └─ runtime/                Token 估算、计时等能力
├─ core/config.py             配置加载与校验
└─ bootstrap.py               装配具体实现
```

## 哪些不能随手移动或删除

`local.yaml` 包含当前配置和密钥；备份时仍按私有文件保管。

`data.sqlite` 保存结构化事实。数据库运行时不要单独移动或删除 WAL/SHM 文件。

`assets/`、`indexes/` 的相对路径由程序记录，不要按视觉美观重新排列。

XMind 是目录视图，Excel 是用例视图；修改这些文件不会反向更新 SQLite 事实。

`dev/cache/`、`dev/tests/current/` 属于可再生成的开发产物，应在相关工具退出后清理。

`dev/demo/`、`dev/backups/` 和历史记录不应一概当作垃圾删除，先确认是否仍需保留。

本次整理保留了配置、数据库、资产、索引、结果及诊断的原路径，启动命令不变：

```powershell
.\.venv\Scripts\python.exe -m tc_rag.interfaces.cli start --config workdir/local.yaml
```

正常运行不依赖 `dev/tools/`。本机临时检查工具需要显式导入时，可从项目根目录执行：

```powershell
$env:PYTHONPATH = 'src;workdir/dev/tools/quality-tools;workdir/dev/tools/optional-tools'
python -m pytest -q --ignore=tests/live
python -m ruff check src tests
python -m mypy src
```

pytest 默认复用专用临时目录，避免每轮留下一个顶层目录；需要保留某次记录或并行测试时，
显式指定不同的 `--basetemp=workdir/dev/tests/任务名`。
