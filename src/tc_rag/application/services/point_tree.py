"""测试点分组、排序和树构建纯函数；不访问文件或数据库。"""

from typing import Literal

from tc_rag.domain import models as m
from tc_rag.domain.identity import new_id
from tc_rag.domain.pipeline import Extraction
from tc_rag.domain.review import PointNode, PointTree

STATUS_ORDER = ("ready", "needs_context", "needs_clarification", "not_applicable")


STATUS_LABELS = {
    "ready": "已就绪",
    "needs_context": "待补充上下文",
    "needs_clarification": "待澄清",
    "not_applicable": "不适用",
}


CATEGORY_ORDER = (
    "入口",
    "权限",
    "业务逻辑",
    "字段校验",
    "异常",
    "安全",
    "其他",
)


SECURITY_HINTS = ("安全", "越权", "未授权", "注入", "xss", "csrf", "绕过", "脱敏", "敏感")


ENTRY_HINTS = ("入口", "菜单", "导航", "页面", "列表", "按钮", "弹窗", "tab", "详情", "可见")


def point_feature(
    point: m.TestPoint,
    rule_feature: dict[str, str],
    feature_names: dict[str, str],
) -> str:
    for rule_id in sorted(point.rule_ids):
        name = feature_names.get(rule_feature.get(rule_id, ""))
        if name:
            return name
    return "未归类"


def sort_points(points: list[m.TestPoint], extraction: Extraction) -> list[m.TestPoint]:
    """与 XMind 一致的顺序：状态 → 业务维度 → 功能 → 测试目标。"""
    rule_map = {rule.rule_id: rule for rule in extraction.rules}
    rule_feature = {rule.rule_id: rule.feature_id for rule in extraction.rules}
    feature_names = {feature.feature_id: feature.name for feature in extraction.features}

    def key(point: m.TestPoint) -> tuple[int, int, str, str]:
        status = STATUS_ORDER.index(point.status) if point.status in STATUS_ORDER else 99
        category = point_category(point, rule_map)
        return (
            status,
            CATEGORY_ORDER.index(category),
            point_feature(point, rule_feature, feature_names),
            point.objective,
        )

    return sorted(points, key=key)


def point_category(point: m.TestPoint, rules: dict[str, m.RequirementRule]) -> str:
    """按业务维度归类；安全与入口用原文关键词识别，其余依据规则类型。"""
    statements = [point.objective]
    kinds: set[str] = set()
    for rule_id in point.rule_ids:
        rule = rules.get(rule_id)
        if rule is not None:
            statements.append(rule.statement)
            kinds.add(rule.kind)
    text = " ".join(statements).lower()
    if any(hint in text for hint in SECURITY_HINTS):
        return "安全"
    if "permission" in kinds:
        return "权限"
    if "field_constraint" in kinds:
        return "字段校验"
    if "error_behavior" in kinds:
        return "异常"
    if kinds & {"business_condition", "state_transition", "workflow"}:
        return "业务逻辑"
    if (not kinds or kinds <= {"other"}) and any(hint in text for hint in ENTRY_HINTS):
        return "入口"
    return "其他"


def build_point_tree(
    run_id: str,
    checksum: str,
    extraction: Extraction,
    points: list[m.TestPoint],
    blocks: list[m.DocumentBlock] | None = None,
    *,
    layout: Literal["feature", "chapter", "module"] = "feature",
) -> PointTree:
    """保留全部点与状态/业务维度；chapter 在功能层之上补充原文章节。"""
    root = new_id("node")
    nodes = [PointNode(node_id=root, title="需求测试点", kind="group", order=0)]
    rule_map = {rule.rule_id: rule for rule in extraction.rules}
    rule_feature = {rule.rule_id: rule.feature_id for rule in extraction.rules}
    feature_names = {feature.feature_id: feature.name for feature in extraction.features}
    evidence_blocks = {e.evidence_id: e.block_id for e in extraction.evidence}
    block_headings = {
        b.block_id: tuple(title.strip() for title in b.heading_path if title.strip())
        for b in blocks or []
    }

    def feature_of(point: m.TestPoint) -> str:
        return point_feature(point, rule_feature, feature_names)

    order = 0

    def group(parent_id: str, title: str) -> str:
        nonlocal order
        node_id = new_id("node")
        nodes.append(PointNode(
            node_id=node_id, parent_id=parent_id, title=title, kind="group", order=order
        ))
        order += 1
        return node_id

    def chapter_of(point: m.TestPoint) -> tuple[str, ...]:
        paths = {
            block_headings.get(evidence_blocks.get(evidence_id, ""), ())
            for rule_id in point.rule_ids
            if rule_id in rule_map
            for evidence_id in rule_map[rule_id].evidence_ids
        }
        if not paths or paths == {()}:
            return ("unlocated",)
        # 全部直接依据共同确定章节；不能让 rule_ids/evidence_ids 的顺序决定归属。
        common = list(min(paths))
        for path in paths:
            common = common[:next(
                (index for index, (a, b) in enumerate(zip(common, path)) if a != b),
                min(len(common), len(path)),
            )]
        return ("source", *common) if common else ("cross",)

    if layout == "module":
        block_order = {block.block_id: block.order for block in blocks or []}

        def source_order(point: m.TestPoint) -> int:
            orders = [
                block_order[evidence_blocks[evidence_id]]
                for rule_id in point.rule_ids
                if rule_id in rule_map
                for evidence_id in rule_map[rule_id].evidence_ids
                if evidence_id in evidence_blocks
                and evidence_blocks[evidence_id] in block_order
            ]
            return min(orders, default=10**9)

        modules: dict[tuple[str, ...], list[m.TestPoint]] = {}
        for point in points:
            path = chapter_of(point)
            if path == ("unlocated",):
                path = ("feature", feature_of(point))
            modules.setdefault(path, []).append(point)
        module_nodes: dict[tuple[str, ...], str] = {}

        def ensure_module(path: tuple[str, ...]) -> str:
            if path in module_nodes:
                return module_nodes[path]
            if path and path[0] == "source":
                parent = root
                prefix: tuple[str, ...] = ("source",)
                for title in path[1:]:
                    prefix = (*prefix, title)
                    if prefix not in module_nodes:
                        module_nodes[prefix] = group(parent, title)
                    parent = module_nodes[prefix]
                module_nodes[path] = parent
                return parent
            if path and path[0] == "feature":
                title = path[1] if len(path) > 1 else "未定位模块"
            else:
                title = "跨模块" if path and path[0] == "cross" else "未定位模块"
            module_nodes[path] = group(root, title)
            return module_nodes[path]

        ordered_modules = sorted(
            modules.items(),
            key=lambda item: (
                min(source_order(point) for point in item[1]),
                item[0],
            ),
        )
        for path, module_points in ordered_modules:
            module_node = ensure_module(path)
            for category in CATEGORY_ORDER:
                module_category_points = [
                    point
                    for point in module_points
                    if point_category(point, rule_map) == category
                ]
                if not module_category_points:
                    continue
                module_category_node = group(
                    module_node, f"{category}（{len(module_category_points)}）"
                )
                module_by_feature: dict[str, list[m.TestPoint]] = {}
                for point in module_category_points:
                    module_by_feature.setdefault(feature_of(point), []).append(point)
                ordered_features = sorted(
                    module_by_feature,
                    key=lambda name: min(
                        source_order(point) for point in module_by_feature[name]
                    ),
                )
                for feature_name in ordered_features:
                    module_feature_points = sorted(
                        module_by_feature[feature_name],
                        key=lambda point: (source_order(point), point.objective),
                    )
                    module_feature_node = group(
                        module_category_node,
                        f"{feature_name}（{len(module_feature_points)}）",
                    )
                    for module_point_order, point in enumerate(module_feature_points):
                        nodes.append(
                            PointNode(
                                node_id=new_id("node"),
                                parent_id=module_feature_node,
                                title=point.objective,
                                kind="point",
                                point_id=point.point_id,
                                technique=point.technique,
                                status=point.status,
                                order=module_point_order,
                            )
                        )
        return PointTree(
            tree_id=new_id("tree"),
            version=1,
            run_id=run_id,
            document_fingerprint=checksum,
            nodes=nodes,
        )

    for status in STATUS_ORDER:
        status_points = [point for point in points if point.status == status]
        if not status_points:
            continue
        status_node = group(root, f"{STATUS_LABELS[status]}（{len(status_points)}）")
        for category in CATEGORY_ORDER:
            category_points = [
                point
                for point in status_points
                if point_category(point, rule_map) == category
            ]
            if not category_points:
                continue
            category_node = group(status_node, f"{category}（{len(category_points)}）")
            by_chapter: dict[tuple[str, ...], list[m.TestPoint]] = {}
            for point in category_points:
                chapter = chapter_of(point) if layout == "chapter" else ()
                by_chapter.setdefault(chapter, []).append(point)
            chapter_nodes: dict[tuple[str, ...], str] = {(): category_node}
            for chapter, chapter_points in sorted(by_chapter.items()):
                parent = category_node
                if chapter:
                    titles = (
                        chapter[1:] if chapter[0] == "source"
                        else ("跨章节",) if chapter[0] == "cross" else ("未定位章节",)
                    )
                    for index, title in enumerate(titles, 1):
                        key = (*chapter[:1], *titles[:index])
                        if key not in chapter_nodes:
                            chapter_nodes[key] = group(parent, title)
                        parent = chapter_nodes[key]
                by_feature: dict[str, list[m.TestPoint]] = {}
                for point in chapter_points:
                    by_feature.setdefault(feature_of(point), []).append(point)
                for feature_name in sorted(by_feature):
                    feature_points = sorted(
                        by_feature[feature_name], key=lambda point: point.objective
                    )
                    feature_node = group(parent, f"{feature_name}（{len(feature_points)}）")
                    for point_order, point in enumerate(feature_points):
                        nodes.append(PointNode(
                            node_id=new_id("node"), parent_id=feature_node,
                            title=point.objective, kind="point", point_id=point.point_id,
                            technique=point.technique, status=point.status,
                            order=point_order,
                        ))
    return PointTree(
        tree_id=new_id("tree"),
        version=1,
        run_id=run_id,
        document_fingerprint=checksum,
        nodes=nodes,
    )
