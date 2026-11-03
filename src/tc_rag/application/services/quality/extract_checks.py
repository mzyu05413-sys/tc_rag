"""提炼结果的证据、规则与引用闭包检查。"""

import re

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
from tc_rag.application.services.design import is_ui_display_rule
from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import blocks as _blocks
from tc_rag.application.services.quality._common import evidence_by_id as _evidence_by_id
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn
from tc_rag.domain import models as m


def _extract_checks(
    snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    extract_payload = snapshots.get("extract")
    chunk_payload = snapshots.get("chunk")

    def evidence_check() -> Check:
        if extract_payload is None or chunk_payload is None:
            return _na("Q-EX-01", "extract", "缺少 extract/chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        invalid: list[str] = []
        for item in extract_payload.get("evidence", []):
            evidence = m.EvidenceSpan.model_validate(item)
            block = blocks.get(evidence.block_id)
            if block is None or not verify_evidence(evidence, block):
                invalid.append(evidence.evidence_id)
        if invalid:
            return _fail(
                "Q-EX-01",
                "extract",
                "引文不在对应 Block 中或定位/来源不一致",
                counts={
                    "evidence": len(extract_payload.get("evidence", [])),
                    "invalid": len(invalid),
                },
                object_ids=invalid,
            )
        return _pass(
            "Q-EX-01",
            "extract",
            "所有引文均可回到 Block 核验",
            counts={"evidence": len(extract_payload.get("evidence", []))},
        )

    def closure_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-02", "extract", "缺少 extract 快照")
        evidence = set(_evidence_by_id(extract_payload))
        bad_features = [
            feature.get("feature_id", "")
            for feature in extract_payload.get("features", [])
            if not set(feature.get("evidence_ids", [])) <= evidence
        ]
        bad_entities = [
            entity.get("entity_id", "")
            for entity in extract_payload.get("entities", [])
            if not set(entity.get("evidence_ids", [])) <= evidence
        ]
        if bad_features or bad_entities:
            return _fail(
                "Q-EX-02",
                "extract",
                "功能或实体引用了不存在的证据",
                counts={"bad_features": len(bad_features), "bad_entities": len(bad_entities)},
                object_ids=bad_features + bad_entities,
            )
        return _pass(
            "Q-EX-02",
            "extract",
            "功能与实体证据引用闭包完整",
            counts={
                "features": len(extract_payload.get("features", [])),
                "entities": len(extract_payload.get("entities", [])),
            },
        )

    def rule_closure() -> Check:
        if extract_payload is None:
            return _na("Q-EX-03", "extract", "缺少 extract 快照")
        evidence = set(_evidence_by_id(extract_payload))
        features = {
            feature.get("feature_id") for feature in extract_payload.get("features", [])
        }
        bad: list[str] = []
        for rule in extract_payload.get("rules", []):
            if (
                rule.get("feature_id") not in features
                or not set(rule.get("evidence_ids", [])) <= evidence
            ):
                bad.append(rule.get("rule_id", ""))
        if bad:
            return _fail(
                "Q-EX-03",
                "extract",
                "规则引用了不存在的功能或证据",
                counts={"rules": len(extract_payload.get("rules", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-EX-03",
            "extract",
            "规则功能/证据引用闭包完整",
            counts={"rules": len(extract_payload.get("rules", []))},
        )

    def constraint_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-04", "extract", "缺少 extract 快照")
        evidence = _evidence_by_id(extract_payload)
        bad_boundary: list[str] = []
        bad_unit: list[str] = []
        bad_precision: list[str] = []
        for item in extract_payload.get("rules", []):
            rule = m.RequirementRule.model_validate(item)
            constraint = rule.structured_constraint
            if constraint is None:
                continue
            quote = normalized_quote(
                "\n".join(
                    evidence[e].quote for e in rule.evidence_ids if e in evidence
                )
            )
            if constraint.boundary is not None and normalized_quote(
                str(constraint.boundary)
            ) not in quote:
                bad_boundary.append(rule.rule_id)
            if constraint.unit and normalized_quote(constraint.unit) not in quote:
                bad_unit.append(rule.rule_id)
            if constraint.precision is not None:
                reliable = (
                    bool(re.search(r"整数|[个条项字符]", quote))
                    if constraint.precision == 0
                    else bool(re.search(rf"小数\s*{constraint.precision}\s*位", quote))
                )
                if not reliable:
                    bad_precision.append(rule.rule_id)
        if bad_boundary:
            return _fail(
                "Q-EX-04",
                "extract",
                "结构化边界数值未在引文中出现",
                counts={"bad_boundary": len(bad_boundary)},
                object_ids=bad_boundary,
            )
        if bad_unit or bad_precision:
            return _warn(
                "Q-EX-04",
                "extract",
                "结构化单位或精度缺少明确依据",
                counts={"bad_unit": len(bad_unit), "bad_precision": len(bad_precision)},
                object_ids=sorted(set(bad_unit + bad_precision)),
            )
        return _pass(
            "Q-EX-04",
            "extract",
            "结构化约束均有引文依据",
            counts={"rules": len(extract_payload.get("rules", []))},
        )

    def issue_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-05", "extract", "缺少 extract 快照")
        invalid = [
            item
            for item in extract_payload.get("issues", [])
            if item.get("kind") == "validation"
        ]
        if invalid:
            return _warn(
                "Q-EX-05",
                "extract",
                "提炼阶段存在引用/定位校验问题",
                counts={"validation_issues": len(invalid)},
                object_ids=sorted(
                    {
                        str(obj)
                        for item in invalid
                        for obj in item.get("object_ids", [])
                    }
                ),
            )
        return _pass("Q-EX-05", "extract", "提炼阶段无引用校验问题")

    def skipped_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-06", "extract", "缺少 extract 快照")
        skipped = [
            item
            for item in extract_payload.get("issues", [])
            if "已跳过" in str(item.get("description", ""))
        ]
        if skipped:
            return _warn(
                "Q-EX-06",
                "extract",
                "存在因提炼失败而跳过的分块，规则可能不完整",
                counts={"skipped_chunks": len(skipped)},
                object_ids=[
                    str(obj)
                    for item in skipped
                    for obj in item.get("object_ids", [])
                ][:50],
            )
        return _pass("Q-EX-06", "extract", "没有因失败跳过的分块")

    def kind_ratio_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-07", "extract", "缺少 extract 快照")
        raw_rules = extract_payload.get("rules", [])
        if not raw_rules:
            return _pass("Q-EX-07", "extract", "没有规则需要分类")
        evidence = {
            item["evidence_id"]: m.EvidenceSpan.model_validate(item)
            for item in extract_payload.get("evidence", [])
        }
        rules = [m.RequirementRule.model_validate(item) for item in raw_rules]
        ui_rules = [rule for rule in rules if is_ui_display_rule(rule, evidence)]
        business = [rule for rule in rules if rule not in ui_rules]
        other_business = sum(1 for rule in business if rule.kind == "other")
        ratio = other_business / len(business) if business else 0.0
        counts = {
            "rules": len(rules),
            "ui_display": len(ui_rules),
            "business": len(business),
            "other_business": other_business,
            "ratio": round(ratio, 3),
        }
        if business and ratio > 0.4:
            return _warn(
                "Q-EX-07",
                "extract",
                "排除纯界面规则后 other 仍偏高，建议人工核对分类",
                counts=counts,
            )
        return _pass("Q-EX-07", "extract", "规则分类比例可用", counts=counts)

    return [
        _safe("Q-EX-01", "extract", schema_errors, evidence_check),
        _safe("Q-EX-02", "extract", schema_errors, closure_check),
        _safe("Q-EX-03", "extract", schema_errors, rule_closure),
        _safe("Q-EX-04", "extract", schema_errors, constraint_check),
        _safe("Q-EX-05", "extract", schema_errors, issue_check),
        _safe("Q-EX-06", "extract", schema_errors, skipped_check),
        _safe("Q-EX-07", "extract", schema_errors, kind_ratio_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
extract_checks = _extract_checks
