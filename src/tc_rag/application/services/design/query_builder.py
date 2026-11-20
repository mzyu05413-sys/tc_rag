"""两阶段 RAG 查询构造：把功能背景、规则关键词与测试点目标组织成稳定结构。

查询只使用批次内已有的生产输入（指令、规则、测试点、功能），不拼入尚未知晓的
金标答案。文档版本、范围与来源过滤由检索适配器的 scope 和 direct_ids 负责，不
重复写进查询字符串，避免稀释语义目标。
"""

from collections.abc import Mapping, Sequence

from tc_rag.domain.models import Feature, RequirementRule, TestPoint


def _feature_names(
    rules: Sequence[RequirementRule], features: Mapping[str, Feature]
) -> list[str]:
    """按规则归属去重功能名；缺失或空白功能名跳过，不编造功能背景。"""
    names: list[str] = []
    for feature_id in sorted({rule.feature_id for rule in rules}):
        feature = features.get(feature_id)
        if feature is not None and feature.name.strip():
            names.append(feature.name.strip())
    return names


def build_plan_query(
    instruction: str,
    rules: Sequence[RequirementRule],
    features: Mapping[str, Feature],
) -> str:
    """规划阶段查询：指令（操作与场景）+ 功能背景 + 本批规则关键词。"""
    parts = [instruction.strip()] if instruction.strip() else []
    parts.extend(f"功能：{name}" for name in _feature_names(rules, features))
    parts.extend(rule.statement.strip() for rule in rules if rule.statement.strip())
    return "\n".join(parts)


def build_case_query(
    points: Sequence[TestPoint],
    rules: Mapping[str, RequirementRule],
    features: Mapping[str, Feature],
) -> str:
    """用例阶段查询：功能背景 + 测试点目标；目标已包含其引用规则的语义。"""
    rule_ids = sorted({rule_id for point in points for rule_id in point.rule_ids})
    batch_rules = [rules[rule_id] for rule_id in rule_ids if rule_id in rules]
    parts = [f"功能：{name}" for name in _feature_names(batch_rules, features)]
    parts.extend(point.objective.strip() for point in points if point.objective.strip())
    return "\n".join(parts)
