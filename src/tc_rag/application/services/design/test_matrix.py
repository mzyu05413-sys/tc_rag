"""测试设计矩阵：从同功能、互补类型的规则对生成有依据的组合测试点。

组合只发生在同一功能内、规则类型互补的规则对之间，避免跨功能或任意两两组合
造成的组合爆炸与无依据断言。组合点默认 status=needs_clarification：组合关系是
从“同功能”推断的候选，不等于原文已明确声明该交互，需人工确认后才是确定断言。
"""

from collections.abc import Sequence
from typing import Literal

from tc_rag.domain.identity import new_id
from tc_rag.domain.models import RequirementRule, TestPoint

# 组合技术限定为 TestPoint.technique 的合法子集。
_ComboTechnique = Literal["permission", "decision_table"]

# 互补类型对 -> 组合技术；只组合这些跨类型规则，不组合同类型或任意类型。
_COMBINATIONS: dict[tuple[str, str], _ComboTechnique] = {
    ("permission", "state_transition"): "permission",
    ("permission", "workflow"): "permission",
    ("field_constraint", "business_condition"): "decision_table",
}


def combination_points(
    rules: Sequence[RequirementRule], techniques: Sequence[str]
) -> list[TestPoint]:
    """生成同功能内互补类型规则对的组合候选，按启用方法过滤技术。"""
    by_feature: dict[str, list[RequirementRule]] = {}
    for rule in rules:
        by_feature.setdefault(rule.feature_id, []).append(rule)

    points: list[TestPoint] = []
    for feature_rules in by_feature.values():
        for (left_kind, right_kind), technique in _COMBINATIONS.items():
            if technique not in techniques:
                continue
            lefts = [r for r in feature_rules if r.kind == left_kind]
            rights = [r for r in feature_rules if r.kind == right_kind]
            for left in lefts:
                for right in rights:
                    points.append(
                        TestPoint(
                            point_id=new_id("point"),
                            rule_ids=sorted([left.rule_id, right.rule_id]),
                            technique=technique,
                            objective=f"{left.statement}；且 {right.statement}",
                            applicability="undetermined",
                            status="needs_clarification",
                        )
                    )
    return points
