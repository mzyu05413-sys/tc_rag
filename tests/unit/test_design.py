"""虚构原文验证来源、边界值和覆盖闭包，不使用用户需求资料。"""

import json

import pytest

from tc_rag.application.orchestration.pipeline import sort_points
from tc_rag.application.services.chunking import structural_chunks, verify_evidence
from tc_rag.application.services.design import (
    boundary_values,
    check_extraction,
    derive_constraint,
    plan_points,
    validate_cases,
)
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction


def block(text="最多300个，数量为整数。", **kwargs):
    return m.DocumentBlock(
        block_id="b",
        document_id="d",
        kind="paragraph",
        order=0,
        content=text,
        locator=m.Locator(page=1),
        **kwargs,
    )


def rule(boundary=300, precision=0):
    return m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="field_constraint",
        statement="最多300个",
        evidence_ids=["e"],
        structured_constraint=m.StructuredConstraint(
            field_path="数量", operator="<=", boundary=boundary, precision=precision
        ),
    )


@pytest.mark.parametrize(
    "boundary,precision,expected",
    [
        (300, 0, ["299", "300", "301"]),
        ("3.00", 2, ["2.99", "3.00", "3.01"]),
        (300, None, []),
        ("NaN", 0, []),
        ("1.25", 1, []),
    ],
)
def test_boundary_precision(boundary, precision, expected):
    assert boundary_values(rule(boundary, precision)) == expected


def _with_operator(operator, boundary=10, precision=0):
    return rule(boundary, precision).model_copy(
        update={
            "structured_constraint": m.StructuredConstraint(
                field_path="数量", operator=operator, boundary=boundary, precision=precision
            )
        }
    )


@pytest.mark.parametrize("operator", ["≤", "不超过", "最多", "le", "<="])
def test_boundary_values_normalizes_operator_aliases(operator):
    assert boundary_values(_with_operator(operator)) == ["9", "10", "11"]


def test_boundary_values_supports_max_length_without_precision():
    rule_obj = rule(16, None).model_copy(
        update={
            "structured_constraint": m.StructuredConstraint(
                field_path="status.name", operator="max_length", boundary=16, precision=None
            )
        }
    )
    assert boundary_values(rule_obj) == ["15", "16", "17"]


def test_program_points_do_not_embed_technique_in_objective():
    points = plan_points([rule(300, 0)], ["equivalence", "boundary"])
    assert all("方法=" not in point.objective for point in points)
    assert any("边界输入=300" in point.objective for point in points)


def test_namespace_allows_reused_local_ids_across_types():
    from tc_rag.application.orchestration.pipeline import Pipeline

    candidate = Extraction(
        evidence=[
            m.EvidenceSpan(evidence_id="1", block_id="b", locator=m.Locator(page=1), quote="q")
        ],
        features=[m.Feature(feature_id="1", name="f", description="", evidence_ids=["1"])],
        rules=[
            m.RequirementRule(
                rule_id="1",
                feature_id="1",
                kind="other",
                statement="s",
                evidence_ids=["1"],
            )
        ],
    )
    result = Pipeline._namespace(candidate, "p")
    assert result.evidence[0].evidence_id == "p_evidence_1"
    assert result.features[0].feature_id == "p_features_1"
    assert result.rules[0].rule_id == "p_rules_1"
    assert result.features[0].evidence_ids == ["p_evidence_1"]
    assert result.rules[0].feature_id == "p_features_1"


def test_extraction_rejects_duplicate_ids_within_same_type():
    with pytest.raises(ValueError, match="evidence IDs must be unique"):
        Extraction(
            evidence=[
                m.EvidenceSpan(evidence_id="1", block_id="b", locator=m.Locator(page=1), quote="a"),
                m.EvidenceSpan(evidence_id="1", block_id="b", locator=m.Locator(page=1), quote="b"),
            ]
        )


def test_namespace_preserves_typed_and_ambiguous_references():
    """所有类型同号时按字段映射；无类型问题关联存在歧义时保留原值。"""
    from tc_rag.application.orchestration.pipeline import Pipeline

    candidate = Extraction(
        evidence=[
            m.EvidenceSpan(evidence_id="1", block_id="1", locator=m.Locator(page=1), quote="q"),
            m.EvidenceSpan(evidence_id="e2", block_id="1", locator=m.Locator(page=1), quote="q2"),
        ],
        features=[m.Feature(feature_id="1", name="f", description="", evidence_ids=["1", "e2"])],
        rules=[
            m.RequirementRule(
                rule_id="1", feature_id="1", kind="other", statement="s", evidence_ids=["1"]
            )
        ],
        entities=[
            m.BusinessEntity(entity_id="1", kind="field", name="field", evidence_ids=["1", "e2"])
        ],
        issues=[
            m.Issue(
                issue_id="1",
                kind="gap",
                severity="warning",
                description="gap",
                object_ids=["1", "e2", "unknown"],
            )
        ],
    )
    result = Pipeline._namespace(candidate, "p")
    ids = {
        result.evidence[0].evidence_id,
        result.features[0].feature_id,
        result.rules[0].rule_id,
        result.entities[0].entity_id,
        result.issues[0].issue_id,
    }
    assert len(ids) == 5
    assert result.evidence[0].block_id == "1"
    assert result.rules[0].evidence_ids == [result.evidence[0].evidence_id]
    assert result.rules[0].feature_id == result.features[0].feature_id
    assert result.entities[0].evidence_ids == [value.evidence_id for value in result.evidence]
    assert result.issues[0].object_ids == ["1", result.evidence[1].evidence_id, "unknown"]
    assert candidate.evidence[0].evidence_id == "1"  # 不原位修改调用方对象。
    assert Pipeline._namespace(candidate, "other").rules[0].rule_id != result.rules[0].rule_id


def test_sort_points_matches_tree_order():
    extraction = Extraction(
        features=[m.Feature(feature_id="f", name="订单", description="", evidence_ids=["e"])],
        rules=[
            m.RequirementRule(
                rule_id="r_field",
                feature_id="f",
                kind="field_constraint",
                statement="金额必须大于0",
                evidence_ids=["e"],
            ),
            m.RequirementRule(
                rule_id="r_perm",
                feature_id="f",
                kind="permission",
                statement="仅管理员可操作",
                evidence_ids=["e"],
            ),
        ],
    )
    points = [
        m.TestPoint(
            point_id="p3",
            rule_ids=["r_field"],
            technique="boundary",
            objective="金额边界",
            status="needs_clarification",
        ),
        m.TestPoint(
            point_id="p2",
            rule_ids=["r_field"],
            technique="equivalence",
            objective="金额校验",
            status="ready",
            applicability="applicable",
        ),
        m.TestPoint(
            point_id="p1",
            rule_ids=["r_perm"],
            technique="permission",
            objective="仅管理员可操作",
            status="ready",
            applicability="applicable",
        ),
    ]
    assert [point.point_id for point in sort_points(points, extraction)] == [
        "p1",
        "p2",
        "p3",
    ]


def test_tree_groups_by_status_then_business_category():
    from tc_rag.application.orchestration.pipeline import Pipeline

    extraction = Extraction(
        features=[m.Feature(feature_id="f", name="订单", description="", evidence_ids=["e"])],
        rules=[
            m.RequirementRule(
                rule_id="r_field",
                feature_id="f",
                kind="field_constraint",
                statement="金额必须大于0",
                evidence_ids=["e"],
            ),
            m.RequirementRule(
                rule_id="r_perm",
                feature_id="f",
                kind="permission",
                statement="仅管理员可操作",
                evidence_ids=["e"],
            ),
        ],
    )
    points = [
        m.TestPoint(
            point_id="p1",
            rule_ids=["r_perm"],
            technique="permission",
            objective="仅管理员可操作",
            status="ready",
            applicability="applicable",
        ),
        m.TestPoint(
            point_id="p2",
            rule_ids=["r_field"],
            technique="equivalence",
            objective="金额校验",
            status="ready",
            applicability="applicable",
        ),
        m.TestPoint(
            point_id="p3",
            rule_ids=["r_field"],
            technique="boundary",
            objective="金额边界",
            status="needs_clarification",
        ),
    ]
    tree = Pipeline._tree("run", "hash", extraction, points)
    titles = [node.title for node in tree.nodes]
    assert "已就绪（2）" in titles and "待澄清（1）" in titles
    assert "权限（1）" in titles and "字段校验（1）" in titles
    assert titles.index("权限（1）") < titles.index("字段校验（1）")
    assert titles.index("已就绪（2）") < titles.index("权限（1）")


def test_check_extraction_derives_numeric_constraint_from_quote():
    text = "审批意见最多300字符。"
    block = m.DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0, content=text,
        locator=m.Locator(page=1),
    )
    evidence = m.EvidenceSpan(evidence_id="e", block_id="b", locator=block.locator, quote=text)
    feature = m.Feature(feature_id="f", name="长度", description="", evidence_ids=["e"])
    rule = m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="field_constraint",
        statement="审批意见最多300字符",
        evidence_ids=["e"],
    )
    result = check_extraction(
        Extraction(evidence=[evidence], features=[feature], rules=[rule]), [block]
    )
    constraint = result.rules[0].structured_constraint
    assert constraint is not None
    assert constraint.operator == "<=" and constraint.boundary == 300
    assert constraint.precision == 0
    assert boundary_values(result.rules[0]) == ["299", "300", "301"]


def test_derive_constraint_supports_length_and_rejects_conflicts():
    length = derive_constraint("状态名称长度限制为16")
    assert length is not None
    assert length.operator == "<=" and length.boundary == 16 and length.precision == 0
    assert derive_constraint("最多300个，至少10个") is None


def test_evidence_fullwidth_whitespace_and_wrong_location():
    original = block()
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=original.locator, quote="最多 ３００ 个"
    )
    assert verify_evidence(evidence, original)
    assert not verify_evidence(evidence.model_copy(update={"locator": m.Locator(page=2)}), original)
    assert not verify_evidence(evidence.model_copy(update={"quote": "最多301个"}), original)


def test_invalid_evidence_cannot_reach_rules():
    candidate = Extraction(
        evidence=[
            m.EvidenceSpan(evidence_id="e", block_id="b", locator=m.Locator(page=1), quote="虚构")
        ],
        features=[m.Feature(feature_id="f", name="数量", description="", evidence_ids=["e"])],
        rules=[rule()],
    )
    checked = check_extraction(candidate, [block()])
    assert checked.rules == [] and checked.features == []
    assert len(checked.issues) == 3


def test_chunk_source_and_exception_not_split():
    first = block("每次最多300个。")
    exception = first.model_copy(update={"block_id": "b2", "order": 1, "content": "但管理员除外。"})
    result = structural_chunks([first, exception], 10, 0)
    assert len(result.chunks) == 1
    assert result.issues
    assert result.chunks[0].text == first.content + "\n" + exception.content
    assert [s.block_id for s in result.chunks[0].sources] == ["b", "b2"]


def test_large_table_repeats_first_row_with_exact_offsets():
    text = json.dumps(
        {"rows": [["字段", "限制"]] + [[f"字段{i}", "最多300个"] for i in range(20)]},
        ensure_ascii=False,
    )
    original = block(text).model_copy(update={"kind": "table"})
    result = structural_chunks([original], 70, 0)
    assert len(result.chunks) > 1
    for chunk in result.chunks:
        slices = [text[s.start_char : s.end_char] for s in chunk.sources]
        assert chunk.text == "\n".join(slices)
        assert slices[0] == '["字段", "限制"]'


def test_planning_unknown_precision_requires_clarification():
    points = plan_points([rule(300, None)], ["boundary", "equivalence"])
    assert next(p for p in points if p.technique == "boundary").status == "needs_clarification"
    assert len(plan_points([rule()], ["boundary"])) == 3


def test_validation_rejects_cross_run_reference_and_reports_gap():
    points = plan_points([rule()], ["boundary"])
    case = m.TestCase(
        case_id="c",
        point_ids=[points[0].point_id],
        title="数量299",
        steps=[
            m.TestStep(action="输入299", expected="成功", rule_ids=["foreign"], evidence_ids=["e"])
        ],
    )
    valid, links, issues = validate_cases([case], points, [rule()], [])
    assert valid == [] and links == []
    assert sum(i.kind == "coverage" for i in issues) == 3


def test_explanatory_expectation_is_advisory_not_blocking():
    r = rule()
    point = plan_points([r], ["boundary"])[0]
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=m.Locator(), quote="最多300个"
    )
    case = m.TestCase(
        case_id="c",
        point_ids=[point.point_id],
        title="边界拒绝",
        steps=[
            m.TestStep(
                action="输入299个", expected="系统拒绝提交", rule_ids=["r"], evidence_ids=["e"]
            )
        ],
    )
    valid, _, issues = validate_cases([case], [point], [r], [evidence])
    assert valid[0].status == "validated"
    assert any(item.severity == "info" for item in issues)


def test_unsupported_expectation_and_duplicate_are_retained_and_flagged():
    r = rule()
    point = plan_points([r], ["boundary"])[0]
    evidence = m.EvidenceSpan(evidence_id="e", block_id="b", locator=m.Locator(), quote="最多300个")
    case = m.TestCase(
        case_id="c1",
        point_ids=[point.point_id],
        title="输入299",
        steps=[
            m.TestStep(
                action="输入299个", expected="返回错误码999", rule_ids=["r"], evidence_ids=["e"]
            )
        ],
    )
    copied = case.model_copy(update={"case_id": "c2"})
    valid, links, issues = validate_cases([case, copied], [point], [r], [evidence])
    assert len(valid) == 2 and len(links) == 2
    assert all(c.status == "needs_review" for c in valid)
    assert {i.kind for i in issues} >= {"unsupported_expectation", "duplicate"}


def test_precision_not_in_quote_is_removed():
    original = block("温度不能超过300。")
    candidate = Extraction(
        evidence=[
            m.EvidenceSpan(
                evidence_id="e", block_id="b", locator=original.locator, quote=original.content
            )
        ],
        features=[m.Feature(feature_id="f", name="温度", description="", evidence_ids=["e"])],
        rules=[rule()],
    )
    checked = check_extraction(candidate, [original])
    assert checked.rules[0].structured_constraint.precision is None
    assert checked.issues[0].kind == "gap"
