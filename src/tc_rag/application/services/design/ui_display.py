"""识别与归并纯界面展示点，保留规则和证据。"""

import re
from collections.abc import Mapping
from typing import Literal

from tc_rag.application.services.design._common import _resolved_rule_evidence, issue
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import EvidenceSpan, Feature, Issue, RequirementRule, TestPoint

_UI_DISPLAY_HINTS = (
    # 只认强展示标记；单凭“页面/列表/按钮”不能判定为纯展示。
    "界面",
    "显示",
    "展示",
    "呈现",
    "标题为",
    "位于",
    "高亮",
    "图标",
    "颜色",
    "配色",
    "布局",
    "样式",
    "字体",
    "文案",
    "标签",
    "表头",
    "菜单",
    "链接",
    "占位",
    "左上角",
    "右上角",
    "左下角",
    "右下角",
    "底部",
    "顶部",
    "列名",
)


_UI_FUNCTION_HINTS = (
    # 只有可执行动作才算功能点；外观描述里的名词（删除/分页/状态）不再否决合并。
    "点击",
    "输入",
    "提交",
    "保存",
    "校验",
    "必填",
    "跳转",
    "上传",
    "下载",
)


def is_ui_display_rule(
    rule: RequirementRule,
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """纯界面展示规则：无数字约束、只有图片外观证据、无可执行动作。"""
    if rule.kind != "other" or rule.structured_constraint is not None:
        return False
    items = [evidence[item] for item in rule.evidence_ids if item in evidence]
    if not items or any(item.origin != "vision" for item in items):
        return False
    if re.search(r"\d", rule.statement):
        return False
    if any(hint in rule.statement for hint in _UI_FUNCTION_HINTS):
        return False
    return any(hint in rule.statement for hint in _UI_DISPLAY_HINTS)


def plan_ui_display_summary(
    rules: list[RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
) -> tuple[list[TestPoint], list[Issue]]:
    """纯界面规则按功能收敛为一条展示汇总点。"""
    groups: dict[str, list[RequirementRule]] = {}
    for rule in rules:
        if is_ui_display_rule(rule, evidence):
            groups.setdefault(rule.feature_id, []).append(rule)
    points: list[TestPoint] = []
    issues: list[Issue] = []
    for feature_id, feature_rules in groups.items():
        name = features[feature_id].name if feature_id in features else "未归类"
        rule_ids = sorted(rule.rule_id for rule in feature_rules)
        objective = (
            feature_rules[0].statement
            if len(feature_rules) == 1
            else f"{name}：界面展示样式与内容正确"
        )
        points.append(
            TestPoint(
                point_id=new_id("point"),
                rule_ids=rule_ids,
                technique="other",
                objective=objective,
                status="ready",
                applicability="applicable",
                display_summary=True,
            )
        )
        issues.append(
            issue(
                "other",
                "纯界面展示规则已在规划前归并汇总，不再逐条生成测试点",
                rule_ids,
                severity="info",
            )
        )
    return points, issues


def is_ui_display_point(
    point: TestPoint,
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """纯界面展示点：vision 外观证据、无功能行为、无结构化约束。"""
    if point.display_summary:
        return False
    if point.technique not in {"other", "equivalence"}:
        return False
    resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
    if resolved is None:
        return False
    point_rules, point_evidence = resolved
    if len({rule.feature_id for rule in point_rules}) != 1:
        return False
    if any(
        rule.kind != "other" or rule.structured_constraint is not None
        for rule in point_rules
    ):
        return False
    if any(item.origin != "vision" for item in point_evidence):
        return False
    text = point.objective
    if not any(hint in text for hint in _UI_DISPLAY_HINTS):
        return False
    if any(hint in text for hint in _UI_FUNCTION_HINTS):
        return False
    return True


def _merged_display_state(group: list[TestPoint]) -> tuple[str, str]:
    statuses = {point.status for point in group}
    if "needs_clarification" in statuses:
        return "needs_clarification", "undetermined"
    if "needs_context" in statuses:
        return "needs_context", "undetermined"
    if all(point.status == "not_applicable" for point in group):
        return "not_applicable", "not_applicable"
    if all(point.applicability == "applicable" for point in group):
        return "ready", "applicable"
    return "needs_context", "undetermined"


def merge_ui_display_points(
    points: list[TestPoint],
    rules: Mapping[str, RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
    policy: Literal["merge", "keep"] = "merge",
) -> tuple[list[TestPoint], list[Issue]]:
    """把同一功能的纯界面展示点替换为一条概括点；规则和证据引用保留在汇总点。"""
    if policy != "merge" or not points:
        return list(points), []
    groups: dict[str, list[TestPoint]] = {}
    for point in points:
        if not is_ui_display_point(point, rules, evidence):
            continue
        feature_id = rules[point.rule_ids[0]].feature_id
        groups.setdefault(feature_id, []).append(point)
    if not groups:
        return list(points), []

    merged_by_first: dict[str, TestPoint] = {}
    dropped: set[str] = set()
    issues: list[Issue] = []
    for feature_id, group in groups.items():
        first = group[0]
        rule_ids = sorted({rule_id for point in group for rule_id in point.rule_ids})
        name = features[feature_id].name if feature_id in features else "未归类"
        status, applicability = _merged_display_state(group)
        merged_by_first[first.point_id] = first.model_copy(
            update={
                "rule_ids": rule_ids,
                "technique": "other",
                "objective": f"{name}：界面展示样式与内容正确",
                "status": status,
                "applicability": applicability,
                "display_summary": True,
            }
        )
        dropped.update(point.point_id for point in group[1:])
        description = (
            f"将同一功能的 {len(group)} 个纯界面展示测试点归并为 1 个汇总测试点"
            if len(group) > 1
            else "纯界面展示测试点已改写为功能汇总点"
        )
        issues.append(
            issue(
                "other",
                description,
                [point.point_id for point in group],
                severity="info",
            )
        )
    result = [
        merged_by_first.get(point.point_id, point)
        for point in points
        if point.point_id not in dropped
    ]
    return result, issues
