"""纯界面展示点识别与合并，不依赖模型或真实数据库。"""

import pytest

from tc_rag.application.services.design import (
    is_ui_display_point,
    merge_ui_display_points,
)
from tc_rag.domain import models as m


def feature(feature_id: str = "f1", name: str = "产品列表") -> m.Feature:
    return m.Feature(
        feature_id=feature_id, name=name, description="", evidence_ids=["e1"]
    )


def evidence(evidence_id: str = "e1", origin: str = "vision") -> m.EvidenceSpan:
    return m.EvidenceSpan(
        evidence_id=evidence_id,
        block_id="b1",
        locator=m.Locator(page=1),
        quote="产品列表界面展示字段A、字段B。",
        origin=origin,
        processing_model="fixture" if origin == "vision" else None,
        processing_version="v1" if origin == "vision" else None,
    )


def rule(
    rule_id: str,
    statement: str,
    feature_id: str = "f1",
    kind: str = "other",
    evidence_ids: list[str] | None = None,
    constraint: m.StructuredConstraint | None = None,
) -> m.RequirementRule:
    return m.RequirementRule(
        rule_id=rule_id,
        feature_id=feature_id,
        kind=kind,
        statement=statement,
        structured_constraint=constraint,
        evidence_ids=evidence_ids or ["e1"],
        status="needs_review",
    )


def point(
    point_id: str,
    rule_ids: list[str],
    objective: str,
    *,
    technique: str = "other",
    status: str = "ready",
    applicability: str = "applicable",
) -> m.TestPoint:
    return m.TestPoint(
        point_id=point_id,
        rule_ids=rule_ids,
        technique=technique,
        objective=objective,
        applicability=applicability,
        status=status,
    )


def test_multiple_display_points_merge_into_one_summary():
    points = [
        point("p1", ["r1"], "字段A、字段B展示"),
        point("p2", ["r2"], "列C、列D展示"),
    ]
    rules = {
        "r1": rule("r1", "产品列表展示字段A、字段B"),
        "r2": rule("r2", "产品列表展示列C、列D"),
    }
    merged, issues = merge_ui_display_points(
        points, rules, {"f1": feature()}, {"e1": evidence()}
    )
    assert len(merged) == 1
    assert merged[0].point_id == "p1"
    assert merged[0].rule_ids == ["r1", "r2"]
    assert merged[0].technique == "other"
    assert merged[0].objective == "产品列表：界面展示样式与内容正确"
    assert issues[0].severity == "info"
    assert set(issues[0].object_ids) == {"p1", "p2"}
    # 输入不被修改。
    assert points[0].objective == "字段A、字段B展示"


def test_single_display_point_is_rewritten():
    points = [point("p1", ["r1"], "字段A展示")]
    merged, issues = merge_ui_display_points(
        points,
        {"r1": rule("r1", "产品列表展示字段A")},
        {"f1": feature()},
        {"e1": evidence()},
    )
    assert len(merged) == 1
    assert merged[0].objective == "产品列表：界面展示样式与内容正确"
    assert issues[0].description == "纯界面展示测试点已改写为功能汇总点"


def test_functional_rule_is_not_merged():
    points = [point("p1", ["r1"], "字段A展示", technique="equivalence")]
    rules = {"r1": rule("r1", "产品列表展示字段A", kind="field_constraint")}
    merged, issues = merge_ui_display_points(
        points, rules, {"f1": feature()}, {"e1": evidence()}
    )
    assert merged == points and issues == []


def test_text_evidence_is_not_merged():
    points = [point("p1", ["r1"], "字段A展示")]
    merged, issues = merge_ui_display_points(
        points,
        {"r1": rule("r1", "产品列表展示字段A")},
        {"f1": feature()},
        {"e1": evidence(origin="text")},
    )
    assert merged == points and issues == []


def test_mixed_evidence_is_not_merged():
    points = [point("p1", ["r1", "r2"], "字段A展示")]
    rules = {
        "r1": rule("r1", "产品列表展示字段A", evidence_ids=["e1"]),
        "r2": rule("r2", "产品列表展示字段B", evidence_ids=["e2"]),
    }
    merged, issues = merge_ui_display_points(
        points,
        rules,
        {"f1": feature()},
        {"e1": evidence(), "e2": evidence("e2", origin="text")},
    )
    assert merged == points and issues == []


def test_multi_feature_point_is_not_merged():
    points = [point("p1", ["r1", "r2"], "字段A展示")]
    rules = {
        "r1": rule("r1", "产品列表展示字段A"),
        "r2": rule("r2", "合同列表展示字段B", feature_id="f2"),
    }
    merged, issues = merge_ui_display_points(
        points,
        rules,
        {"f1": feature(), "f2": feature("f2", "合同列表")},
        {"e1": evidence()},
    )
    assert merged == points and issues == []


def test_structured_constraint_is_not_merged():
    points = [point("p1", ["r1"], "字段A展示")]
    rules = {
        "r1": rule(
            "r1",
            "产品列表展示字段A",
            constraint=m.StructuredConstraint(boundary=300),
        )
    }
    merged, issues = merge_ui_display_points(
        points, rules, {"f1": feature()}, {"e1": evidence()}
    )
    assert merged == points and issues == []


def test_boundary_technique_is_not_merged():
    points = [point("p1", ["r1"], "字段A展示", technique="boundary")]
    merged, issues = merge_ui_display_points(
        points,
        {"r1": rule("r1", "产品列表展示字段A")},
        {"f1": feature()},
        {"e1": evidence()},
    )
    assert merged == points and issues == []


def test_status_and_applicability_aggregate_worst_first():
    points = [
        point("p1", ["r1"], "字段A展示"),
        point(
            "p2",
            ["r2"],
            "字段B展示",
            status="needs_clarification",
            applicability="undetermined",
        ),
    ]
    merged, _ = merge_ui_display_points(
        points,
        {
            "r1": rule("r1", "产品列表展示字段A"),
            "r2": rule("r2", "产品列表展示字段B"),
        },
        {"f1": feature()},
        {"e1": evidence()},
    )
    assert merged[0].status == "needs_clarification"
    assert merged[0].applicability == "undetermined"


def test_all_not_applicable_stays_not_applicable():
    points = [
        point(
            "p1",
            ["r1"],
            "字段A展示",
            status="not_applicable",
            applicability="not_applicable",
        )
    ]
    merged, _ = merge_ui_display_points(
        points,
        {"r1": rule("r1", "产品列表展示字段A")},
        {"f1": feature()},
        {"e1": evidence()},
    )
    assert merged[0].status == "not_applicable"
    assert merged[0].applicability == "not_applicable"


def test_keep_policy_returns_unchanged():
    points = [point("p1", ["r1"], "字段A展示")]
    merged, issues = merge_ui_display_points(
        points,
        {"r1": rule("r1", "产品列表展示字段A")},
        {"f1": feature()},
        {"e1": evidence()},
        policy="keep",
    )
    assert merged == points and issues == []


def test_order_and_survivor_id_are_preserved():
    points = [
        point("p0", ["r0"], "功能校验", technique="equivalence"),
        point("p1", ["r1"], "字段A展示"),
        point("p2", ["r2"], "字段B展示"),
        point("p3", ["r3"], "功能状态流转", technique="state"),
    ]
    rules = {
        "r0": rule("r0", "功能校验", kind="business_condition"),
        "r1": rule("r1", "产品列表展示字段A"),
        "r2": rule("r2", "产品列表展示字段B"),
        "r3": rule("r3", "状态流转", kind="state_transition"),
    }
    merged, _ = merge_ui_display_points(
        points, rules, {"f1": feature()}, {"e1": evidence()}
    )
    assert [item.point_id for item in merged] == ["p0", "p1", "p3"]
    assert merged[1].objective == "产品列表：界面展示样式与内容正确"


def test_detection_requires_display_marker_and_no_function_marker():
    rules = {"r1": rule("r1", "产品列表展示字段A")}
    assert is_ui_display_point(
        point("p1", ["r1"], "字段A展示"), rules, {"e1": evidence()}
    )
    assert not is_ui_display_point(
        point("p1", ["r1"], "点击按钮后展示字段A"), rules, {"e1": evidence()}
    )


@pytest.mark.parametrize(
    "objective",
    [
        "产品列表页标题为“产品管理”。",
        "页面左上角提供青绿色的“新建产品”按钮。",
        "删除确认弹窗标题为“确认删除产品XXX吗？”，标题左侧有橙色圆形感叹号警示图标。",
        "列表底部分页位于右侧，显示页码1至5，当前页1青绿色高亮，左右各有翻页箭头。",
        "表头设置显示全部字段。",
    ],
)
def test_descriptive_appearance_points_are_display(objective):
    rules = {"r1": rule("r1", "产品列表界面说明")}
    assert is_ui_display_point(point("p1", ["r1"], objective), rules, {"e1": evidence()})


@pytest.mark.parametrize(
    "objective",
    [
        "列表支持通过产品名称搜索。",
        "点击保存后展示字段A。",
        "新建产品时产品状态必填。",
        "产品管理支持产品价格、基本信息维护。",
    ],
)
def test_functional_points_are_not_display(objective):
    rules = {"r1": rule("r1", "产品列表界面说明")}
    assert not is_ui_display_point(point("p1", ["r1"], objective), rules, {"e1": evidence()})
