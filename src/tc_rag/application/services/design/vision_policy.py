"""图片来源策略、已解决图片问题识别与清理。"""

import json
from collections.abc import Mapping
from typing import Literal

from tc_rag.application.services.design._common import _resolved_rule_evidence, issue
from tc_rag.domain.models import (
    DocumentBlock,
    EvidenceSpan,
    Feature,
    Issue,
    RequirementRule,
    TestPoint,
)


def is_vision_only(
    point: TestPoint,
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> bool:
    """测试点引用的全部证据都来自图片解释等非正文来源。"""
    resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
    if resolved is None:
        return False
    _, point_evidence = resolved
    return bool(point_evidence) and all(
        item.origin != "text" or item.uncertain for item in point_evidence
    )


def apply_vision_policy(
    points: list[TestPoint],
    rules: Mapping[str, RequirementRule],
    features: Mapping[str, Feature],
    evidence: Mapping[str, EvidenceSpan],
    policy: Literal["aggregate", "downgrade", "keep"] = "aggregate",
) -> tuple[list[TestPoint], list[Issue]]:
    """仅图片证据默认不生成测试点；每个功能汇总一条待补充问题。"""
    if not points or policy == "keep":
        return list(points), []
    grouped: dict[str, list[str]] = {}
    output: list[TestPoint] = []
    for point in points:
        if not is_vision_only(point, rules, evidence):
            output.append(point)
            continue
        if point.display_summary or "界面展示汇总" in point.objective:
            output.append(point)
            continue
        if policy == "aggregate" and point.technique == "boundary":
            output.append(
                point.model_copy(
                    update={"status": "needs_context", "applicability": "undetermined"}
                )
            )
            continue
        if policy == "downgrade":
            if point.status not in {"needs_clarification", "not_applicable"}:
                point = point.model_copy(
                    update={"status": "needs_context", "applicability": "undetermined"}
                )
            output.append(point)
        resolved = _resolved_rule_evidence(point.rule_ids, rules, evidence)
        feature_id = resolved[0][0].feature_id if resolved is not None else "未归类"
        grouped.setdefault(feature_id, []).append(point.point_id)
    issues = [
        issue(
            "other",
            f"{features[feature_id].name if feature_id in features else feature_id}："
            + (
                "测试点仅有图片证据，未生成测试点，待补充文字依据"
                if policy == "aggregate"
                else "测试点仅有图片证据，待文本依据核对"
            ),
            point_ids,
            severity="info",
        )
        for feature_id, point_ids in sorted(grouped.items())
    ]
    return output, issues


def resolved_image_asset_ids(blocks: list[DocumentBlock]) -> set[str]:
    """列出已有 vision 派生块的图片资产，供问题撤销与质量检查共用。"""
    vision_ids = [block.block_id for block in blocks if block.origin == "vision"]
    resolved: set[str] = set()
    for image in (block for block in blocks if block.kind == "image"):
        if not any(item.startswith(image.block_id + "_vision_") for item in vision_ids):
            continue
        try:
            asset = json.loads(image.content)
        except (TypeError, ValueError):
            continue
        if isinstance(asset, dict) and asset.get("asset_id"):
            resolved.add(str(asset["asset_id"]))
    return resolved


def _mentions_image_asset(item: Issue, asset_ids: set[str]) -> bool:
    """解析阶段的图片问题用 domain 定位，资产 ID 只在描述中；两种形态都识别。"""
    return any(obj in asset_ids for obj in item.object_ids) or any(
        asset_id in item.description for asset_id in asset_ids
    )


def drop_resolved_image_issues(
    issues: list[Issue], blocks: list[DocumentBlock]
) -> list[Issue]:
    """图片已有 vision 派生块时，移除解析阶段遗留的“未解析”问题。"""
    resolved = resolved_image_asset_ids(blocks)
    # 确定性过滤不代表识别成功，但对应解析占位告警不应继续称为识别缺失。
    # 单独保留 info 跳过记录，绝不把该图片加入 resolved_image_asset_ids。
    image_assets: set[str] = set()
    for block in blocks:
        if block.kind != "image":
            continue
        try:
            asset = json.loads(block.content)
        except (TypeError, ValueError):
            continue
        if isinstance(asset, dict) and asset.get("asset_id"):
            image_assets.add(str(asset["asset_id"]))
    resolved |= {
        obj for item in issues
        if item.severity == "info" and item.description == "图片过小，已跳过"
        for obj in item.object_ids if obj in image_assets
    }
    if not resolved:
        return list(issues)
    return [
        item
        for item in issues
        if not (item.kind == "unparsed" and _mentions_image_asset(item, resolved))
    ]
