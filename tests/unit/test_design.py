"""虚构原文验证来源、边界值和覆盖闭包，不使用用户需求资料。"""

import json

import pytest

from tc_rag.application.orchestration.pipeline import point_category, sort_points
from tc_rag.application.services.chunking import structural_chunks, verify_evidence
from tc_rag.application.services.design import (
    apply_vision_policy,
    boundary_values,
    case_signature,
    check_extraction,
    dedupe_issues,
    deduplicate_points,
    derive_constraint,
    drop_resolved_image_issues,
    error_rule_ids,
    plan_points,
    qualitative_boundary_values,
    reconcile,
    unsupported_numbers,
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
    boundary = [point for point in points if point.technique == "boundary"]
    assert len(boundary) == 1
    assert boundary[0].data_variants == ["299", "300", "301"]
    assert "边界数据=299/300/301" in boundary[0].objective


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


def test_document_title_heading_is_not_a_rule():
    title = m.DocumentBlock(
        block_id="b0",
        document_id="d",
        kind="heading",
        order=0,
        content="洋么科技需求文档",
        locator=m.Locator(paragraph_index=0),
    )
    evidence = m.EvidenceSpan(
        evidence_id="e0",
        block_id="b0",
        locator=title.locator,
        quote=title.content,
    )
    feature = m.Feature(feature_id="f0", name="文档", description="", evidence_ids=["e0"])
    rule = m.RequirementRule(
        rule_id="r0",
        feature_id="f0",
        kind="other",
        statement="文档标题为“洋么科技需求文档”，当前内容不包含可执行的功能行为或约束。",
        evidence_ids=["e0"],
    )
    checked = check_extraction(
        Extraction(evidence=[evidence], features=[feature], rules=[rule]), [title]
    )
    assert checked.rules == []
    assert checked.evidence[0].evidence_id == "e0"
    assert any("文档标题" in item.description for item in checked.issues)


def test_error_issue_blocks_rule_planning():
    rule_obj = rule()
    blocked = m.Issue(
        issue_id="i1",
        kind="gap",
        severity="error",
        description="无法生成有效测试点",
        object_ids=[rule_obj.rule_id],
    )
    warning = m.Issue(
        issue_id="i2",
        kind="gap",
        severity="warning",
        description="建议核对",
        object_ids=[rule_obj.rule_id],
    )
    assert error_rule_ids([blocked, warning], [rule_obj]) == {rule_obj.rule_id}
    assert error_rule_ids([warning], [rule_obj]) == set()


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


def test_planning_unknown_precision_skips_boundary_point():
    points = plan_points([rule(300, None)], ["boundary", "equivalence"])
    assert all(point.technique != "boundary" for point in points)
    assert any(point.technique == "equivalence" for point in points)
    assert len(plan_points([rule()], ["boundary"])) == 1


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
    assert sum(i.kind == "coverage" for i in issues) == 1


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


def _vision_evidence(eid: str = "e1") -> m.EvidenceSpan:
    return m.EvidenceSpan(
        evidence_id=eid,
        block_id="b1",
        locator=m.Locator(page=1),
        quote="产品列表界面说明",
        origin="vision",
        processing_model="fixture",
        processing_version="v1",
    )


def _vision_point(status: str = "ready") -> m.TestPoint:
    return m.TestPoint(
        point_id="p1",
        rule_ids=["r1"],
        technique="other",
        objective="产品列表界面展示",
        applicability="applicable",
        status=status,
    )


def _vision_rule(evidence_ids: list[str] | None = None) -> m.RequirementRule:
    return m.RequirementRule(
        rule_id="r1",
        feature_id="f1",
        kind="other",
        statement="产品列表界面说明",
        evidence_ids=evidence_ids or ["e1"],
        status="needs_review",
    )


def _feature() -> m.Feature:
    return m.Feature(feature_id="f1", name="产品列表", description="", evidence_ids=["e1"])


def test_vision_only_point_is_downgraded():
    out, issues = apply_vision_policy(
        [_vision_point()],
        {"r1": _vision_rule()},
        {"f1": _feature()},
        {"e1": _vision_evidence()},
        policy="downgrade",
    )
    assert out[0].status == "needs_context"
    assert out[0].applicability == "undetermined"
    assert [item.object_ids for item in issues] == [["p1"]]
    assert issues[0].severity == "info"


def test_vision_only_point_is_omitted_by_default():
    out, issues = apply_vision_policy(
        [_vision_point()],
        {"r1": _vision_rule()},
        {"f1": _feature()},
        {"e1": _vision_evidence()},
    )
    assert out == []
    assert issues[0].object_ids == ["p1"]
    assert "未生成测试点" in issues[0].description


def test_vision_boundary_point_is_kept_as_needs_context():
    point = _vision_point().model_copy(update={"technique": "boundary"})
    out, issues = apply_vision_policy(
        [point],
        {"r1": _vision_rule()},
        {"f1": _feature()},
        {"e1": _vision_evidence()},
    )
    assert [item.status for item in out] == ["needs_context"]
    assert issues == []


def test_mixed_text_and_vision_evidence_stays_ready():
    text = m.EvidenceSpan(evidence_id="e2", block_id="b1", locator=m.Locator(), quote="正文")
    out, issues = apply_vision_policy(
        [_vision_point()],
        {"r1": _vision_rule(["e1", "e2"])},
        {"f1": _feature()},
        {"e1": _vision_evidence(), "e2": text},
    )
    assert out[0].status == "ready"
    assert issues == []


def test_vision_keep_policy_returns_unchanged():
    out, issues = apply_vision_policy(
        [_vision_point()],
        {"r1": _vision_rule()},
        {"f1": _feature()},
        {"e1": _vision_evidence()},
        policy="keep",
    )
    assert out[0].status == "ready"
    assert issues == []


def test_resolved_image_issue_is_removed():
    image = m.DocumentBlock(
        block_id="b1",
        document_id="d",
        kind="image",
        order=0,
        content=json.dumps({"asset_id": "a1"}),
        locator=m.Locator(page=1),
    )
    derived = m.DocumentBlock(
        block_id="b1_vision_fp",
        document_id="d",
        kind="paragraph",
        order=1,
        content="图片解释",
        locator=m.Locator(page=1),
        origin="vision",
        processing_model="fixture",
        processing_version="v1",
    )
    resolved = m.Issue(
        issue_id="i1",
        kind="unparsed",
        severity="warning",
        description="图片 a1 仅提取与定位，图片文字尚未解析",
        object_ids=["doc"],
    )
    unresolved = m.Issue(
        issue_id="i2",
        kind="unparsed",
        severity="warning",
        description="图片 a2 仅提取与定位，图片文字尚未解析",
        object_ids=["doc"],
    )
    out = drop_resolved_image_issues([resolved, unresolved], [image, derived])
    assert [item.issue_id for item in out] == ["i2"]


def test_unparsed_image_issue_remains_without_vision_block():
    image = m.DocumentBlock(
        block_id="b1",
        document_id="d",
        kind="image",
        order=0,
        content=json.dumps({"asset_id": "a1"}),
        locator=m.Locator(page=1),
    )
    issue = m.Issue(
        issue_id="i1",
        kind="unparsed",
        severity="warning",
        description="图片 a1 仅提取与定位",
        object_ids=["a1"],
    )
    assert drop_resolved_image_issues([issue], [image]) == [issue]


def _extraction(rule_id: str, statement: str, evidence_id: str) -> Extraction:
    return Extraction(
        evidence=[
            m.EvidenceSpan(
                evidence_id=evidence_id,
                block_id="b1",
                locator=m.Locator(),
                quote=statement,
            )
        ],
        features=[
            m.Feature(feature_id="f", name="图片上传", description="", evidence_ids=[evidence_id])
        ],
        rules=[
            m.RequirementRule(
                rule_id=rule_id,
                feature_id="f",
                kind="other",
                statement=statement,
                evidence_ids=[evidence_id],
            )
        ],
    )


def test_reconcile_merges_near_duplicate_rules():
    merged = reconcile(
        [
            _extraction("r1", "产品图片大小需在5MB内。", "e1"),
            _extraction("r2", "产品图片大小须在5MB以内。", "e2"),
        ]
    )
    assert len(merged.rules) == 1
    assert merged.rule_mapping["r1"] == merged.rule_mapping["r2"]


def test_reconcile_keeps_rules_with_different_constraints():
    def with_constraint(rule_id: str, boundary: int):
        extraction = _extraction(rule_id, "最多300个", f"e{boundary}")
        extraction.rules[0].structured_constraint = m.StructuredConstraint(
            field_path="数量", operator="<=", boundary=boundary, precision=0
        )
        return extraction

    merged = reconcile([with_constraint("r1", 300), with_constraint("r2", 500)])
    assert len({rule.rule_id for rule in merged.rules}) == 2


def _point(point_id: str, technique: str, objective: str, status: str = "ready") -> m.TestPoint:
    return m.TestPoint(
        point_id=point_id,
        rule_ids=["r1"],
        technique=technique,
        objective=objective,
        applicability="applicable",
        status=status,
    )


def test_deduplicate_points_collapses_cross_method_duplicates():
    kept, issues = deduplicate_points(
        [
            _point("p1", "other", "产品列表默认按创建时间倒序排序。"),
            _point("p2", "decision_table", "产品列表默认按创建时间倒序排序。"),
        ]
    )
    assert len(kept) == 1
    assert kept[0].technique == "decision_table"
    assert [item.kind for item in issues] == ["duplicate"]


def test_deduplicate_points_merges_near_identical_wording():
    kept, _ = deduplicate_points(
        [
            _point("p1", "equivalence", "产品图片大小需在 5MB 内。"),
            _point("p2", "boundary", "产品图片大小须在5MB以内。"),
        ]
    )
    assert len(kept) == 1
    assert kept[0].technique == "boundary"


def test_deduplicate_points_keeps_distinct_boundary_values():
    kept, issues = deduplicate_points(
        [
            _point("p1", "boundary", "边界输入=299"),
            _point("p2", "boundary", "边界输入=300"),
        ]
    )
    assert len(kept) == 2
    assert issues == []


def test_dedupe_issues_merges_identical_descriptions():
    merged = dedupe_issues(
        [
            m.Issue(
                issue_id="i1",
                kind="gap",
                severity="warning",
                description="测试点不适用或仍待澄清，未生成用例",
                object_ids=["p1"],
            ),
            m.Issue(
                issue_id="i2",
                kind="gap",
                severity="warning",
                description="测试点不适用或仍待澄清，未生成用例",
                object_ids=["p2"],
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].object_ids == ["p1", "p2"]


def test_case_signature_normalizes_whitespace_and_width():
    first = m.TestCase(
        case_id="c1",
        point_ids=["p1"],
        title="用例",
        steps=[
            m.TestStep(action="输入 300 个", expected="拒绝", rule_ids=["r"], evidence_ids=["e"])
        ],
    )
    second = m.TestCase(
        case_id="c2",
        point_ids=["p1"],
        title="用例",
        steps=[
            m.TestStep(action="输入300个", expected="拒绝", rule_ids=["r"], evidence_ids=["e"])
        ],
    )
    assert case_signature(first) == case_signature(second)


def test_coverage_link_validated_for_grounded_step():
    constraint = rule()
    point = plan_points([constraint], ["boundary"])[0]
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=m.Locator(), quote="最多300个"
    )
    case = m.TestCase(
        case_id="c1",
        point_ids=[point.point_id],
        title="输入300",
        steps=[
            m.TestStep(
                action="输入300个", expected="最多300个", rule_ids=["r"], evidence_ids=["e"]
            )
        ],
    )
    valid, links, _ = validate_cases([case], [point], [constraint], [evidence])
    assert valid[0].status == "validated"
    assert {link.validation_status for link in links} == {"validated"}


def test_coverage_link_needs_review_for_unsupported_expectation():
    constraint = rule()
    point = plan_points([constraint], ["boundary"])[0]
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=m.Locator(), quote="最多300个"
    )
    case = m.TestCase(
        case_id="c1",
        point_ids=[point.point_id],
        title="输入300",
        steps=[
            m.TestStep(
                action="输入300个", expected="返回错误码999", rule_ids=["r"], evidence_ids=["e"]
            )
        ],
    )
    valid, links, _ = validate_cases([case], [point], [constraint], [evidence])
    assert valid[0].status == "needs_review"
    assert {link.validation_status for link in links} == {"needs_review"}


def test_duplicate_case_links_are_needs_review():
    constraint = rule()
    point = plan_points([constraint], ["boundary"])[0]
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=m.Locator(), quote="最多300个"
    )
    case = m.TestCase(
        case_id="c1",
        point_ids=[point.point_id],
        title="输入300",
        steps=[
            m.TestStep(
                action="输入300个", expected="最多300个", rule_ids=["r"], evidence_ids=["e"]
            )
        ],
    )
    copied = case.model_copy(update={"case_id": "c2"})
    valid, links, _ = validate_cases([case, copied], [point], [constraint], [evidence])
    assert [link.validation_status for link in links] == ["validated", "needs_review"]
    assert valid[1].status == "needs_review"


def test_derive_constraint_parses_size_and_count_units():
    size = derive_constraint("产品图片大小需在5MB内。")
    assert size is not None
    assert (size.boundary, size.unit, size.precision) == (5, "MB", None)
    count = derive_constraint("最多上传100张图片。")
    assert count is not None
    assert (count.boundary, count.unit, count.precision) == (100, "张", 0)
    duration = derive_constraint("单次导出最多等待5秒。")
    assert duration is not None
    assert (duration.boundary, duration.unit, duration.precision) == (5, "秒", None)


def test_required_rule_does_not_keep_cross_sentence_boundary():
    text = "学习承诺为必填。学习承诺最大输入长度限制35个汉字。"
    original = block(text)
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=original.locator, quote=text
    )
    feature = m.Feature(feature_id="f", name="学习承诺", description="", evidence_ids=["e"])
    required = m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="field_constraint",
        statement="学习承诺为必填。",
        evidence_ids=["e"],
        structured_constraint=m.StructuredConstraint(
            operator="<=", boundary=35, precision=0, unit="个"
        ),
    )
    checked = check_extraction(
        Extraction(evidence=[evidence], features=[feature], rules=[required]), [original]
    )
    constraint = checked.rules[0].structured_constraint
    assert constraint is not None
    assert constraint.boundary is None and constraint.unit is None
    points = plan_points(checked.rules, ["equivalence", "boundary"])
    assert [point.technique for point in points] == ["equivalence"]
    assert any("跨句边界" in item.description for item in checked.issues)


def test_unsupported_numbers_normalizes_time_and_data():
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=m.Locator(), quote="时间为09:00至23:30"
    )
    time_step = m.TestStep(
        action="查看时间范围", expected="9:00至23:30", rule_ids=["r"], evidence_ids=["e"]
    )
    assert unsupported_numbers(time_step, {}, {"e": evidence}) == []

    count_step = m.TestStep(
        action="输入内容", expected="20个汉字可保存", rule_ids=["r"], evidence_ids=["e"]
    )
    assert unsupported_numbers(count_step, {}, {"e": evidence}) == ["20"]
    assert unsupported_numbers(count_step, {}, {"e": evidence}, {"20"}) == []


def test_must_fill_rule_does_not_keep_cross_sentence_boundary():
    text = "请假原因必须填写且最大长度限制35个汉字。"
    original = block(text)
    evidence = m.EvidenceSpan(
        evidence_id="e", block_id="b", locator=original.locator, quote=text
    )
    feature = m.Feature(feature_id="f", name="请假原因", description="", evidence_ids=["e"])
    required = m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="field_constraint",
        statement="请假原因必须填写。",
        evidence_ids=["e"],
        structured_constraint=m.StructuredConstraint(
            operator="<=", boundary=35, precision=0, unit="个"
        ),
    )
    checked = check_extraction(
        Extraction(evidence=[evidence], features=[feature], rules=[required]), [original]
    )
    assert checked.rules[0].structured_constraint is not None
    assert checked.rules[0].structured_constraint.boundary is None
    assert any("跨句边界" in item.description for item in checked.issues)


def test_size_constraint_uses_qualitative_boundaries():
    constraint = m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="field_constraint",
        statement="产品图片大小不超过5MB",
        evidence_ids=["e"],
        structured_constraint=m.StructuredConstraint(
            field_path="产品图片", operator="<=", boundary=5, precision=None, unit="MB"
        ),
    )
    assert boundary_values(constraint) == []
    assert qualitative_boundary_values(constraint) == ["略低于5MB", "等于5MB", "略高于5MB"]
    points = plan_points([constraint], ["boundary"])
    assert len(points) == 1
    assert points[0].status == "ready"
    assert points[0].data_variants == ["略低于5MB", "等于5MB", "略高于5MB"]


def test_point_category_prefers_rule_kind_over_entry_hint():
    exception = m.RequirementRule(
        rule_id="r",
        feature_id="f",
        kind="error_behavior",
        statement="页面提交失败时提示错误",
        evidence_ids=["e"],
    )
    point = m.TestPoint(
        point_id="p",
        rule_ids=["r"],
        technique="error_behavior",
        objective="页面提交失败时提示错误",
        applicability="applicable",
        status="ready",
    )
    assert point_category(point, {"r": exception}) == "异常"
    workflow = exception.model_copy(update={"kind": "workflow"})
    assert point_category(point, {"r": workflow}) == "业务逻辑"
