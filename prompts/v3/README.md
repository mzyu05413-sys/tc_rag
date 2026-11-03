# V3 提示词草案（未接入）

这些文件用于评审提示词组织方式，**不参与运行**：

- `bootstrap.PROMPT_VERSIONS` 仍指向 v2/v1；
- `pyproject.toml` 未打包本目录；
- 现有测试、manifest 和缓存不读取本目录。

组织方式：角色与目标 → 输入边界 → 行为规则 → 输出契约 → 输出前自检。
V3 以当前 v2 为基线（extract/plan_points/generate 的 v2 已包含 v1 全文与界面展示约束），
只调整顺序、分层和结尾自检，不新增业务规则；rag_context/vision/case_skill 以 v1 为基线。

评审重点：

1. 最高优先级规则是否在前、并在结尾自检再次出现；
2. 条件、例外和否定词是否仍用整段保留；
3. 枚举和硬约束是否仍逐字列出；
4. 是否出现重复、冲突或遗漏。

## 本轮修正（仍未接入）

- `plan_points` 明确 `supplemental_evidence` 是检索补充资料，可用于理解背景、条件和例外，
  但不能新增规则、扩大 `rule_ids` 或写成确定预期；
- `extract` 区分 `structured_constraint=null`（无原文约束，含纯展示规则）与
  `entities[].attributes={}`、`rule_mapping={}`，不再泛化为“对象无内容用 {}”；
- `generate` 明确单点用例上限来自输入 `limits.max_cases_per_point`，
  并与 6 步建议、独立边界点分开说明；
- 三份提示词的自检压缩为一行，减少重复约束和固定长度。

