"""查询构造纯函数测试：只验证结构与槽位，不依赖真实模型或检索。"""

from tc_rag.application.services.design.query_builder import (
    build_case_query,
    build_plan_query,
)
from tc_rag.domain import models as m


def feature(feature_id="f1", name="订单管理"):
    return m.Feature(feature_id=feature_id, name=name, description="", evidence_ids=["e1"])


def rule(rule_id="r1", feature_id="f1", kind="permission", statement="仅管理员可删除"):
    return m.RequirementRule(
        rule_id=rule_id,
        feature_id=feature_id,
        kind=kind,
        statement=statement,
        evidence_ids=["e1"],
    )


def point(point_id="p1", rule_ids=("r1",), objective="验证管理员删除权限"):
    return m.TestPoint(point_id=point_id, rule_ids=list(rule_ids), technique="permission",
                       objective=objective)


def test_plan_query_contains_instruction_feature_and_rules():
    query = build_plan_query(
        "生成测试点", [rule()], {"f1": feature()}
    )
    assert query.splitlines() == ["生成测试点", "功能：订单管理", "仅管理员可删除"]


def test_plan_query_dedupes_feature_names_and_skips_blank():
    rules = [
        rule("r1", "f1", statement="规则甲"),
        rule("r2", "f1", statement="规则乙"),
    ]
    features = {"f1": feature(), "f2": feature("f2", "  ")}
    query = build_plan_query("生成测试点", rules, features)
    assert query.splitlines() == ["生成测试点", "功能：订单管理", "规则甲", "规则乙"]


def test_plan_query_skips_empty_instruction():
    assert build_plan_query("  ", [rule()], {"f1": feature()}).splitlines() == [
        "功能：订单管理", "仅管理员可删除"
    ]


def test_case_query_uses_feature_background_and_objectives():
    query = build_case_query([point()], {"r1": rule()}, {"f1": feature()})
    assert query.splitlines() == ["功能：订单管理", "验证管理员删除权限"]


def test_case_query_derives_feature_from_point_rules_and_ignores_unknown_rule():
    query = build_case_query(
        [point(point_id="p1", rule_ids=["r1", "missing"])],
        {"r1": rule()},
        {"f1": feature()},
    )
    assert "功能：订单管理" in query.splitlines()
    assert "验证管理员删除权限" in query.splitlines()


def test_case_query_skips_blank_objective():
    blank = point(objective="   ")
    assert build_case_query([blank], {"r1": rule()}, {"f1": feature()}).splitlines() == [
        "功能：订单管理"
    ]
