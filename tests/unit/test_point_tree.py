"""验证测试点目录按原文章节层级组织，同路径重复功能合并。"""

from tc_rag.application.orchestration.pipeline import Pipeline
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction


def _block(block_id, heading):
    return m.DocumentBlock(
        block_id=block_id,
        document_id="d",
        kind="paragraph",
        heading_path=heading,
        order=0,
        content="x",
        locator=m.Locator(),
    )


def _evidence(evidence_id, block_id):
    return m.EvidenceSpan(
        evidence_id=evidence_id, block_id=block_id, locator=m.Locator(), quote="x"
    )


def _feature(feature_id, name, evidence_ids):
    return m.Feature(feature_id=feature_id, name=name, description="", evidence_ids=evidence_ids)


def _rule(rule_id, feature_id, evidence_ids):
    return m.RequirementRule(
        rule_id=rule_id,
        feature_id=feature_id,
        kind="business_condition",
        statement="s",
        evidence_ids=evidence_ids,
    )


def _point(point_id, rule_ids, objective):
    return m.TestPoint(
        point_id=point_id, rule_ids=rule_ids, technique="decision_table", objective=objective
    )


def _render(tree):
    by_id = {n.node_id: n for n in tree.nodes}
    root = next(n for n in tree.nodes if n.parent_id is None)
    lines = []

    def walk(node_id, depth):
        node = by_id[node_id]
        if node.parent_id is not None:
            lines.append((depth, node.kind, node.title))
        for child in sorted(
            (n for n in tree.nodes if n.parent_id == node_id), key=lambda n: n.order
        ):
            walk(child.node_id, depth + 1)

    walk(root.node_id, 0)
    return lines


def test_tree_nests_by_heading_path_and_merges_duplicates():
    blocks = [
        _block("b1", ["一、功能点", "2.产品列表", "2.1 列表搜索"]),
        _block("b2", ["一、功能点", "2.产品列表", "2.1 列表搜索"]),
        _block("b3", ["一、功能点", "2.产品列表", "2.2 表头设置"]),
    ]
    extraction = Extraction(
        evidence=[_evidence("e1", "b1"), _evidence("e2", "b2"), _evidence("e3", "b3")],
        features=[
            _feature("f1", "列表搜索", ["e1"]),
            _feature("f2", "列表搜索", ["e2"]),
            _feature("f3", "表头设置", ["e3"]),
        ],
        rules=[
            _rule("r1", "f1", ["e1"]),
            _rule("r2", "f2", ["e2"]),
            _rule("r3", "f3", ["e3"]),
        ],
    )
    points = [
        _point("p1", ["r1"], "搜索有结果仅展示匹配行"),
        _point("p2", ["r2"], "搜索无结果展示空"),
        _point("p3", ["r3"], "默认展示全部字段"),
    ]
    tree = Pipeline._tree("run", "chk", extraction, points, blocks)
    titles = [title for _, _, title in _render(tree)]
    assert titles == [
        "一、功能点",
        "2.产品列表",
        "2.1 列表搜索",
        "搜索有结果仅展示匹配行",
        "搜索无结果展示空",
        "2.2 表头设置",
        "默认展示全部字段",
    ]
    # 同名功能只生成一个分组节点，重复测试点都挂在它下面
    groups = [n for n in tree.nodes if n.kind == "group" and n.title == "2.1 列表搜索"]
    assert len(groups) == 1


def test_tree_falls_back_to_feature_name_without_heading():
    blocks = [_block("b1", [])]
    extraction = Extraction(
        evidence=[_evidence("e1", "b1")],
        features=[_feature("f1", "新建产品", ["e1"])],
        rules=[_rule("r1", "f1", ["e1"])],
    )
    tree = Pipeline._tree(
        "run", "chk", extraction, [_point("p1", ["r1"], "必填校验")], blocks
    )
    rendered = _render(tree)
    assert (rendered[0][2], rendered[1][2]) == ("新建产品", "必填校验")
