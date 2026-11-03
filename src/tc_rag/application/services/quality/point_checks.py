"""测试点引用、边界与状态一致性检查。"""

from collections import Counter
from typing import Any

from tc_rag.application.services.design import (
    boundary_values,
    is_ui_display_point,
    qualitative_boundary_values,
)
from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import evidence_by_id as _evidence_by_id
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import rules_by_id as _rules_by_id
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn
from tc_rag.domain import models as m
from tc_rag.domain.review import PointTree


def _point_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    plan_payload = snapshots.get("plan_points")
    extract_payload = snapshots.get("extract")
    tree_payload = snapshots.get("point_tree")

    def closure_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-01", "plan_points", "缺少 plan_points 快照")
        rules = set(_rules_by_id(extract_payload))
        if extract_payload is None:
            return _na("Q-PT-01", "plan_points", "缺少 extract 快照，无法核对规则引用")
        bad = [
            point.get("point_id", "")
            for point in plan_payload.get("points", [])
            if not point.get("rule_ids")
            or not set(point.get("rule_ids", [])) <= rules
        ]
        if bad:
            return _fail(
                "Q-PT-01",
                "plan_points",
                "测试点引用了不存在的规则或规则为空",
                counts={"points": len(plan_payload.get("points", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-PT-01",
            "plan_points",
            "测试点规则引用闭包完整",
            counts={"points": len(plan_payload.get("points", []))},
        )

    def distribution_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-02", "plan_points", "缺少 plan_points 快照")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        statuses = Counter(point.status for point in points)
        inconsistent = sum(
            point.status == "ready" and point.applicability != "applicable"
            for point in points
        )
        counts = {
            "total": len(points),
            "ready": statuses.get("ready", 0),
            "needs_clarification": statuses.get("needs_clarification", 0),
            "needs_context": statuses.get("needs_context", 0),
            "not_applicable": statuses.get("not_applicable", 0),
            "applicability_mismatch": inconsistent,
        }
        if inconsistent:
            return _warn(
                "Q-PT-02",
                "plan_points",
                "存在 ready 但 applicability 不是 applicable 的测试点",
                counts=counts,
                object_ids=[
                    point.point_id
                    for point in points
                    if point.status == "ready" and point.applicability != "applicable"
                ],
            )
        if not points:
            return _warn("Q-PT-02", "plan_points", "没有测试点", counts=counts)
        if not counts["ready"]:
            return _warn(
                "Q-PT-02",
                "plan_points",
                "没有 ready 测试点，后续无法生成确定用例",
                counts=counts,
            )
        return _pass("Q-PT-02", "plan_points", "测试点状态分布可用", counts=counts)

    def boundary_check() -> Check:
        if plan_payload is None or extract_payload is None:
            return _na("Q-PT-03", "plan_points", "缺少 plan_points/extract 快照")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        missing: list[str] = []
        checked = 0
        for rule in _rules_by_id(extract_payload).values():
            if rule.kind != "field_constraint":
                continue
            values = boundary_values(rule)
            if not values:
                values = qualitative_boundary_values(rule)
            if not values:
                continue
            checked += 1
            candidates = [
                point
                for point in points
                if point.technique == "boundary" and rule.rule_id in point.rule_ids
            ]
            if not all(
                any(value in point.objective for point in candidates) for value in values
            ):
                missing.append(rule.rule_id)
        counts = {"rules_with_boundary": checked, "missing": len(missing)}
        if missing:
            return _fail(
                "Q-PT-03",
                "plan_points",
                "可计算数值约束缺少完整的边界测试点",
                counts=counts,
                object_ids=missing,
            )
        return _pass(
            "Q-PT-03", "plan_points", "数值约束的边界测试点完整", counts=counts
        )

    def missing_boundary_check() -> Check:
        if extract_payload is None:
            return _na("Q-PT-04", "plan_points", "缺少 extract 快照")
        missing = [
            rule.rule_id
            for rule in _rules_by_id(extract_payload).values()
            if rule.kind == "field_constraint"
            and not boundary_values(rule)
            and not qualitative_boundary_values(rule)
        ]
        if missing:
            return _warn(
                "Q-PT-04",
                "plan_points",
                "字段约束没有可计算边界，未生成边界测试点",
                counts={"missing": len(missing)},
                object_ids=missing,
            )
        return _pass("Q-PT-04", "plan_points", "字段约束边界可计算")

    def duplicate_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-05", "plan_points", "缺少 plan_points 快照")
        seen: set[tuple[tuple[str, ...], str, str]] = set()
        duplicates: list[str] = []
        for item in plan_payload.get("points", []):
            key = (
                tuple(item.get("rule_ids", [])),
                str(item.get("technique", "")),
                str(item.get("objective", "")),
            )
            if key in seen:
                duplicates.append(item.get("point_id", ""))
            seen.add(key)
        if duplicates:
            return _warn(
                "Q-PT-05",
                "plan_points",
                "存在规则/方法/目标完全重复的测试点",
                counts={"duplicates": len(duplicates)},
                object_ids=duplicates,
            )
        return _pass("Q-PT-05", "plan_points", "测试点无完全重复")

    def tree_check() -> Check:
        if plan_payload is None or tree_payload is None:
            return _na("Q-PT-06", "plan_points", "缺少 plan_points/point_tree 快照")
        tree = PointTree.model_validate(tree_payload)
        planned = {
            str(item.get("point_id")) for item in plan_payload.get("points", [])
        }
        in_tree = {node.point_id for node in tree.nodes if node.kind == "point"}
        if planned != in_tree:
            return _fail(
                "Q-PT-06",
                "plan_points",
                "测试点树与保存的测试点事实不一致",
                counts={"points": len(planned), "tree_points": len(in_tree)},
                object_ids=sorted(planned.symmetric_difference(in_tree)),
            )
        return _pass(
            "Q-PT-06",
            "plan_points",
            "测试点树与测试点事实一致",
            counts={"points": len(planned)},
        )

    def ui_display_check() -> Check:
        if plan_payload is None or extract_payload is None:
            return _na("Q-PT-07", "plan_points", "缺少 plan_points/extract 快照")
        manifest = snapshots.get("manifest") or {}
        version = (
            (manifest.get("prompts") or {}).get("plan_points") or {}
        ).get("version")
        if version != "v2":
            return _na("Q-PT-07", "plan_points", "旧运行未使用 v2 规划提示词")
        policy = (
            (run.get("config_snapshot") or {})
            .get("planning", {})
            .get("ui_display_policy", "merge")
        )
        if policy == "keep":
            return _na("Q-PT-07", "plan_points", "配置为 keep，未要求合并")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        rules = _rules_by_id(extract_payload)
        evidence = _evidence_by_id(extract_payload)
        if not any(item.origin == "vision" for item in evidence.values()):
            return _na("Q-PT-07", "plan_points", "没有 vision 证据")
        display = [point for point in points if is_ui_display_point(point, rules, evidence)]
        if not display:
            return _pass("Q-PT-07", "plan_points", "没有纯界面展示点")
        groups: dict[str, list[m.TestPoint]] = {}
        for point in display:
            feature_ids = {
                rules[rule_id].feature_id
                for rule_id in point.rule_ids
                if rule_id in rules
            }
            if len(feature_ids) == 1:
                groups.setdefault(next(iter(feature_ids)), []).append(point)
        fragmented = [
            point for group in groups.values() if len(group) > 1 for point in group
        ]
        non_summary = [
            point
            for point in display
            if "界面展示汇总" not in point.objective
            and not point.objective.startswith("界面展示：")
        ]
        if fragmented or non_summary:
            return _warn(
                "Q-PT-07",
                "plan_points",
                "仍存在未合并或未概括的纯界面展示点",
                counts={
                    "display_points": len(display),
                    "fragmented": len(fragmented),
                    "non_summary": len(non_summary),
                },
                object_ids=sorted(
                    {point.point_id for point in fragmented + non_summary}
                ),
            )
        return _pass(
            "Q-PT-07",
            "plan_points",
            "纯界面展示点已按功能概括",
            counts={"display_points": len(display)},
        )

    return [
        _safe("Q-PT-01", "plan_points", schema_errors, closure_check),
        _safe("Q-PT-02", "plan_points", schema_errors, distribution_check),
        _safe("Q-PT-03", "plan_points", schema_errors, boundary_check),
        _safe("Q-PT-04", "plan_points", schema_errors, missing_boundary_check),
        _safe("Q-PT-05", "plan_points", schema_errors, duplicate_check),
        _safe("Q-PT-06", "plan_points", schema_errors, tree_check),
        _safe("Q-PT-07", "plan_points", schema_errors, ui_display_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
point_checks = _point_checks
