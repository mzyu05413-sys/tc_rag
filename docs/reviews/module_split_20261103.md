# 大文件拆分评估与交付记录（2026-11-03）

本轮完成阶段 A、B、C；阶段 D 的有状态编排分解尚未实施。拆分以开始时的工作树为基线，保留此前尚未提交的视觉和分块优化，不以旧提交覆盖当前实现。

## 1. 评估结论与范围

指南的 A/B/C 合理：质量检查按阶段独立、设计服务按主题独立，树构建与命名空间函数不依赖 Pipeline 实例，适合机械搬迁。拆分后的旧导入入口保留，收益是更容易阅读、定位和独立维护，不是减少模型调用或提高业务准确率。

D 的方向也有价值，但本轮暂缓：它会重排 `points()` / `cases()` 的局部状态、嵌套闭包、异常边界和恢复分支，不属于同等风险的纯搬迁。按指南建议，先交付 A/B/C，后续先增加阶段事件、失败/取消、恢复及缺失用例补救的特征测试，再单独分解 D。本轮没有用原测试通过来冒充这些特征测试已经完成。

没有修改业务判断、阈值、正则、提示词、日志文案、配置、数据库字段、阶段快照或导出格式。没有调用真实模型、自动提交 Git，未拆 `workflow.py`。`apply_vision_policy`、`Pipeline._chunk`、`points()`、`cases()` 保留原函数正文。

## 2. 实际规模与搬迁

| 原位置（本轮基线） | 新位置 | 结果 |
|---|---|---|
| `services/quality.py`，1912 行 | `services/quality/`，12 个模块 | 最大 `case_checks.py` 为 307 行；原 288 行检查函数不再内部拆分 |
| `orchestration/pipeline.py`，1603 行 | 原文件 + `services/point_tree.py`、`extraction_namespace.py` | pipeline 为 1291 行，减少 312 行；两个纯函数模块分别 300 / 52 行 |
| `services/design.py`，1132 行 | `services/design/`，9 个模块 | 最大 `extraction.py` 为 298 行；其余主题模块约 77～215 行 |

增加包入口、导入和兼容别名后，总行数可能增加；本轮按职责降低单文件复杂度，没有为追求行数压缩原有函数。

### A：质量检查

| 旧函数/内容 | 新模块 |
|---|---|
| Check、SnapshotMap、状态常量、检查构造与快照工具 | `quality/_common.py` |
| `_validate_stage`、`_schema_errors` | `quality/snapshots.py` |
| `_input_checks`、`_parse_checks` | `quality/ingest_checks.py` |
| `_chunk_checks` | `quality/chunk_checks.py` |
| `_extract_checks` | `quality/extract_checks.py` |
| `_point_checks` | `quality/point_checks.py` |
| `_case_checks` | `quality/case_checks.py` |
| `_retrieval_checks` | `quality/retrieval_checks.py` |
| `_invocation_checks` | `quality/invocation_checks.py` |
| `_output_checks` | `quality/output_checks.py` |
| `inspect_quality`、`_overall`、`_missing_stages`、预期阶段常量 | `quality/report.py` |
| 旧公共名称重导出 | `quality/__init__.py` |

与建议的调整：调用记录和输出检查分别放入两个模块，避免它们合成一个新的大文件。跨模块通过 `check`、`passed`、`warn`、`safe` 等内部公共别名导入；检查函数内部沿用旧局部名。这样提供清晰的共享接口，同时完整保留旧定义及函数 AST，不在纯搬迁中改写函数体。

### B：无状态服务

`point_feature`、`sort_points`、`point_category`、相关五组常量及 `_tree` 的实现移入 `point_tree.py`，树构建入口为 `build_point_tree`。

`_namespace` 的实现移入 `extraction_namespace.py`，入口为 `namespace_extraction`。参数名称、位置参数和类型保持原实现；只更改函数的存放位置和独立入口名称。

`_final_status`、`_retrieval_evidence` 和其他有状态方法仍在 Pipeline；没有搬动 `_chunk` 正文。

### C：测试设计

| 内容 | 新模块 |
|---|---|
| issue、归一化、规则证据解析及共享单位常量 | `design/_common.py` |
| 数值正则、单位词表、`derive_constraint` | `design/constraints.py` |
| 提炼核验、状态与冲突处理、`reconcile` | `design/extraction.py` |
| 边界值和确定性测试点规划 | `design/planning.py` |
| 纯展示规则识别和测试点汇总 | `design/ui_display.py` |
| 测试点与问题去重 | `design/dedupe.py` |
| 图片来源策略和已解决图片问题清理 | `design/vision_policy.py` |
| 用例签名、数字依据与用例校验 | `design/case_validation.py` |
| 全部旧公共名重导出 | `design/__init__.py` |

与建议的调整：直接按原划分搬迁时 extraction 约 375 行，因此单列约束推导模块；共用 `_SIZE_UNITS` / `_TIME_UNITS` / `_OPERATOR_ALIASES` 跟随使用关系进入公共模块。所有常量取值不变。

内部依赖为：公共模块在底层；extraction 使用 constraints；case_validation 使用 planning；其余主题通过公共模块共享必要能力，模块间无环。包入口负责兼容导出，主题模块不反向导入包入口。

## 3. 兼容入口

以下现有写法保持可用，现有测试文件没有因拆分而修改：

```python
from tc_rag.application.services.quality import inspect_quality
from tc_rag.application.services.design import issue, plan_points, validate_cases
from tc_rag.application.orchestration.pipeline import point_category, sort_points

Pipeline._tree(...)       # staticmethod(build_point_tree)
Pipeline._namespace(...)  # staticmethod(namespace_extraction)
```

本轮保留全部旧公共名称，包括原模块导入后可见的类型、工具符号和常量，不仅限于文档列出的业务入口。`design` 和 `quality` 的 `__all__` 明确保留这些名称；Pipeline 的旧辅助函数/常量使用显式重新导出。

兼容入口本轮不设置清理期限；以后如需收窄公共 API，应独立评估调用方，不在这次搬迁中顺手删除。

## 4. 验证证据

基线不是指南引用的旧版 652 项，而是本轮开始时实际测得的 **708 passed、2 skipped**。

| 验证点 | 基线 | A 后 | B 后 | C 后 |
|---|---:|---:|---:|---:|
| 完整离线 pytest | 708 / 2 skipped | 708 / 2 skipped | 708 / 2 skipped | 708 / 2 skipped |
| ruff check . | 通过 | 通过 | 通过 | 通过 |
| mypy src（项目已启用 strict） | 66 个文件通过 | 77 个文件通过 | 79 个文件通过 | 87 个文件通过 |

入口导入与模块说明整理后，最终完整验证单独运行于 `workdir/dev/tests/module-split-final`：**708 passed、2 skipped**（73.87 秒），ruff 通过、mypy 通过（87 个源码文件），CLI `--help` 正常。没有添加测试数量来掩盖原测试回归，也没有修改断言。

纯搬迁自动审计：

- quality 的 27 个顶层函数、design 的 36 个顶层函数，AST 忽略行号后与工作树基线一致；常量和类型别名同样一致。
- Pipeline 的 33 个顶层函数/方法均审计：保留方法完全相同；搬出的两个静态方法只归一化函数名和 staticmethod 装饰器，参数和函数体相同。常量也核对保留。
- 原公共名集合均被新路径包含：quality 24 个、design 47 个、pipeline 70 个。
- 同一份合成夹具的干净、缺阶段、错误分块三组 `inspect_quality` JSON 与改前完整相等，包括顺序。
- 新包都有 `__init__.py`。本机未安装 setuptools，未实际构建 wheel；不把源码测试冒充安装包验证，也未为此额外安装依赖。

本机审计材料和源码备份位于：

```text
workdir/dev/maintenance/module-split-20261103/
├─ baseline.json                  原文件规模、旧公共名
├─ quality.before.py              当前工作树拆分前源码
├─ design.before.py
├─ pipeline.before.py
├─ quality-equivalence.json       三组固定输入和拆分前质量报告
├─ audit_A.json / audit_B.json / audit_C.json / audit_final.json
├─ prepare.py                     基线记录脚本，勿在拆分后重新运行覆盖备份
├─ split.py / readability.py       本次机械搬迁工具，不作为日常启动命令
└─ verify.py                      当前源码 AST、常量、公共名和报告等价复核
```

这些材料被 Git 忽略，不包含真实需求或本地密钥。用户此前的需求运行数据没有移动或改写。

复核命令（已激活虚拟环境）：

```powershell
$env:PYTHONPATH = 'src'
python workdir/dev/maintenance/module-split-20261103/verify.py review
python -m pytest -q --basetemp=workdir/dev/tests/module-split-review
python -m ruff check .
python -m mypy src
python -m tc_rag.interfaces.cli --help
```

## 5. 文档同步

已更新目录结构、架构设计、开发指南、P1 提炼/规划/校验三章、完整运行说明、图集代码入口及视觉/分块说明中的当前源码位置。历史日期记录不改写为新的测试结论。

README 增加本记录入口。流程和模型配置没有变化，不需要重建 SQLite 或重新运行需求文档来使用新包布局。

## 6. 回滚边界

三个阶段可以分别回滚；请先保存后续修改，再按文件映射恢复，不能用整仓 `git reset` 覆盖此前未提交的视觉和分块功能。

| 回滚阶段 | 源码操作 |
|---|---|
| A | 将新 `services/quality/` 包移到私有备份目录，再将 `quality.before.py` 复制回 `services/quality.py` |
| B | 将当前 pipeline 和两个新纯函数模块备份移出源码树，再用 `pipeline.before.py` 恢复 pipeline |
| C | 将新 `services/design/` 包移到私有备份目录，再将 `design.before.py` 复制回 `services/design.py` |

不要让同名 `.py` 与带 `__init__.py` 的包同时留在源码路径中，否则包会遮蔽恢复后的模块。回滚源码后同步恢复相应文档路径并运行完整测试；不需要改配置、删数据库或移动业务资产。这里给出回滚映射，没有自动执行回滚。

## 7. 待审核与下一步

请审核主题划分是否便于阅读，以及是否继续保留这些兼容入口。业务行为已有离线测试及结构等价证据，本轮未重新评估真实模型效果，也不声称拆分改善了业务输出。

建议审核 A/B/C 后，单独启动 D：先建立并跑通指南 5.1 的行为特征测试，再保持它们不变地分解有状态编排。没有发现需要在本轮顺手修复的业务缺陷。
