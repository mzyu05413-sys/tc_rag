"""章节是测试点目录的可选视图，不丢状态、功能或待澄清点。"""

from tc_rag.application.orchestration.pipeline import Pipeline
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction


def fixture():
    headings = [["产品", "新增"], ["产品", "新增"], ["产品", "删除"], ["审批"], []]
    blocks, evidence, features, rules = [], [], [], []
    for index, path in enumerate(headings):
        text = f"规则{index}"
        blocks.append(m.DocumentBlock(
            block_id=f"b{index}", document_id="d", kind="paragraph", order=index,
            content=text, locator=m.Locator(paragraph_index=index), heading_path=path,
        ))
        evidence.append(m.EvidenceSpan(
            evidence_id=f"e{index}", block_id=f"b{index}", quote=text,
            locator=blocks[-1].locator,
        ))
        features.append(m.Feature(
            feature_id=f"f{index}", name=f"功能{index}", description="",
            evidence_ids=[f"e{index}"],
        ))
        rules.append(m.RequirementRule(
            rule_id=f"r{index}", feature_id=f"f{index}", kind="permission",
            statement=text, evidence_ids=[f"e{index}"], status="supported",
        ))
    extraction = Extraction(evidence=evidence, features=features, rules=rules)
    points = [m.TestPoint(
        point_id=f"p{i}", rule_ids=[f"r{i}"], technique="permission", objective=f"检查{i}",
        status="ready", applicability="applicable",
    ) for i in range(len(rules))]
    points += [
        m.TestPoint(point_id="common", rule_ids=["r0", "r2"], technique="permission",
                    objective="同一模块的联合要求", status="ready", applicability="applicable"),
        m.TestPoint(point_id="cross", rule_ids=["r0", "r3"], technique="permission",
                    objective="跨模块要求", status="ready", applicability="applicable"),
        m.TestPoint(point_id="pending", rule_ids=["r1"], technique="permission",
                    objective="缺少角色信息", status="needs_clarification"),
        m.TestPoint(point_id="not-applicable", rule_ids=["r2"], technique="permission",
                    objective="本期不适用", status="not_applicable",
                    applicability="not_applicable"),
    ]
    return extraction, points, blocks


def paths(tree):
    nodes = {node.node_id: node for node in tree.nodes}
    result = {}
    for node in tree.nodes:
        if node.kind != "point":
            continue
        titles = []
        parent_id = node.parent_id
        while parent_id:
            parent = nodes[parent_id]
            titles.append(parent.title.split("（")[0])
            parent_id = parent.parent_id
        result[node.point_id] = list(reversed(titles))
    return result


def test_chapter_layout_preserves_all_statuses_and_distinct_features():
    extraction, points, blocks = fixture()
    tree = Pipeline._tree("run", "fingerprint", extraction, points, blocks, layout="chapter")
    actual = paths(tree)
    assert set(actual) == {point.point_id for point in points}
    assert actual["p0"] == ["需求测试点", "已就绪", "权限", "产品", "新增", "功能0"]
    assert actual["p1"] == ["需求测试点", "已就绪", "权限", "产品", "新增", "功能1"]
    assert actual["pending"][1] == "待澄清"
    assert actual["not-applicable"][1] == "不适用"
    assert len([node for node in tree.nodes if node.kind == "point"]) == len(points)


def test_cross_chapter_assignment_uses_all_evidence_and_is_order_independent():
    extraction, points, blocks = fixture()
    before = paths(Pipeline._tree("run", "fp", extraction, points, blocks, layout="chapter"))
    assert before["common"][-2:] == ["产品", "功能0"]
    assert before["cross"][-2:] == ["跨章节", "功能0"]
    assert before["p4"][-2:] == ["未定位章节", "功能4"]
    for point in points:
        point.rule_ids.reverse()
    for rule in extraction.rules:
        rule.evidence_ids.reverse()
    after = paths(Pipeline._tree("run", "fp", extraction, points, blocks, layout="chapter"))
    assert before == after


def test_default_layout_and_missing_chapters_preserve_function_grouping():
    extraction, points, blocks = fixture()
    default = paths(Pipeline._tree("run", "fp", extraction, points, blocks))
    assert default["p0"] == ["需求测试点", "已就绪", "权限", "功能0"]
    missing = paths(Pipeline._tree("run", "fp", extraction, points, layout="chapter"))
    assert missing["p0"][-2:] == ["未定位章节", "功能0"]


def test_same_chapter_and_feature_share_one_group():
    extraction, points, blocks = fixture()
    extraction.features[1].name = extraction.features[0].name
    tree = Pipeline._tree("run", "fp", extraction, points[:2], blocks, layout="chapter")
    leaves = [node for node in tree.nodes if node.kind == "point"]
    assert len(leaves) == 2 and leaves[0].parent_id == leaves[1].parent_id


def test_blank_chapter_names_do_not_create_empty_group_titles():
    extraction, points, blocks = fixture()
    blocks[0].heading_path = [" ", " 产品 ", ""]
    tree = Pipeline._tree("run", "fp", extraction, points[:1], blocks, layout="chapter")
    assert paths(tree)["p0"][-2:] == ["产品", "功能0"]
    assert all(node.title.strip() for node in tree.nodes)


def test_ui_display_summary_point_is_kept_and_grouped_by_feature():
    extraction, points, blocks = fixture()
    summary = m.TestPoint(
        point_id="ui-summary",
        rule_ids=["r0"],
        technique="other",
        objective="功能0：界面展示汇总（合并 2 个纯界面展示点）",
        status="ready",
        applicability="applicable",
    )
    tree = Pipeline._tree("run", "fp", extraction, points + [summary], blocks)
    assert paths(tree)["ui-summary"][-1] == "功能0"
    assert len([node for node in tree.nodes if node.kind == "point"]) == len(points) + 1
