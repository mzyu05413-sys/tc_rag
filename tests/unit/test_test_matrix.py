"""组合矩阵（A4）：同功能内互补类型规则对生成组合候选，默认待澄清。"""

from tc_rag.application.services.design.test_matrix import combination_points
from tc_rag.core.config import Planning
from tc_rag.domain import models as m


def rule(rule_id, feature_id="f", kind="permission", statement="仅管理员可删除"):
    return m.RequirementRule(
        rule_id=rule_id, feature_id=feature_id, kind=kind,
        statement=statement, evidence_ids=["e1"],
    )


def test_complementary_pair_generates_combination_candidate():
    points = combination_points(
        [rule("r1", kind="permission"), rule("r2", kind="state_transition")],
        ["permission"],
    )
    assert len(points) == 1
    point = points[0]
    assert point.rule_ids == ["r1", "r2"]
    assert point.technique == "permission"
    assert point.status == "needs_clarification"
    assert point.applicability == "undetermined"
    assert "仅管理员可删除" in point.objective


def test_same_kind_rules_do_not_combine():
    assert combination_points([rule("r1"), rule("r2", kind="permission")], ["permission"]) == []


def test_different_feature_rules_do_not_combine():
    points = combination_points(
        [rule("r1", feature_id="f1"), rule("r2", feature_id="f2", kind="state_transition")],
        ["permission"],
    )
    assert points == []


def test_disabled_technique_filters_combination():
    assert combination_points(
        [rule("r1"), rule("r2", kind="state_transition")], ["boundary"]
    ) == []


def test_field_condition_pair_uses_decision_table():
    points = combination_points(
        [
            rule("r1", kind="field_constraint", statement="数量最多300"),
            rule("r2", kind="business_condition", statement="订单已提交"),
        ],
        ["decision_table"],
    )
    assert len(points) == 1 and points[0].technique == "decision_table"


def test_planning_config_combination_defaults_off_and_hidden():
    assert Planning().combination_matrix is False
    assert "combination_matrix" not in Planning().model_dump(mode="json")
    assert Planning(combination_matrix=True).model_dump(mode="json")["combination_matrix"] is True
