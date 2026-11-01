# 提示词目录

运行时版本由 [bootstrap.py](../src/tc_rag/bootstrap.py) 的 `PROMPT_VERSIONS` 固定选择，
不会扫描目录并自动使用版本号最大的文件。

| 阶段目录 | 当前装配 | 职责 |
|---|---|---|
| `extract/` | v2 | 提炼功能、规则、实体、证据与问题 |
| `plan_points/` | v2 | 根据规则、直接证据及检索补充资料规划测试点 |
| `generate/` | v2 | 为当前批次点生成 CaseBatch |
| `rag_context/` | v1 | 追加到规划和生成，约束检索资料的用途 |
| `vision/` | v1 | 图片解释，保留不确定性 |
| `case_skill/` | v1 | 追加到生成，约束用例命名、粒度和表达 |
| [candidates/v3](candidates/v3/README.md) | **未装配** | 候选草案，不进入安装包或运行缓存 |

阶段内旧版本为可追溯资源，不能仅因未被默认选择而删除。
目录整理没有切换任何生效提示词，也没有更改提示词正文。
追加规范用 `--skill-file`，不要通过移动候选文件隐式切换生产提示词。
版本与缓存审计见 [提示词说明](../docs/reference/prompt_versions.md)。
