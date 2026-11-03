"""候选提炼校验、精确重复归并与冲突保留。"""

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Literal

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
from tc_rag.application.services.design._common import (
    _normalized_operator,
    _normalized_statement,
    issue,
)
from tc_rag.application.services.design.constraints import derive_constraint
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import DocumentBlock, EvidenceSpan, Feature, Issue, RequirementRule
from tc_rag.domain.pipeline import Extraction

_METADATA_MARKERS = (
    "文档标题",
    "章节标题",
    "仅包含标题",
    "内容不包含",
    "文档包含",
)


def _is_metadata_rule(
    rule: RequirementRule,
    evidence_by_id: Mapping[str, EvidenceSpan],
    block_map: Mapping[str, DocumentBlock],
) -> bool:
    """文档标题/封面/纯章节标题不是可执行需求，不进入规则与测试点。"""
    if rule.kind != "other" or rule.structured_constraint is not None:
        return False
    evidence = [evidence_by_id[item] for item in rule.evidence_ids if item in evidence_by_id]
    blocks = [block_map[item.block_id] for item in evidence if item.block_id in block_map]
    if not blocks or len(blocks) != len(evidence):
        return False
    if any(block.kind != "heading" for block in blocks):
        return False
    return any(marker in rule.statement for marker in _METADATA_MARKERS)


def check_extraction(candidate: Extraction, blocks: list[DocumentBlock]) -> Extraction:
    """拒绝不存在或定位不符的引文，引用闭包失效的规则不会进入生成。"""
    block_map = {b.block_id: b for b in blocks}
    result = Extraction(issues=list(candidate.issues))
    for evidence in candidate.evidence:
        block = block_map.get(evidence.block_id)
        if block is not None and verify_evidence(evidence, block):
            result.evidence.append(evidence)
        else:
            result.issues.append(
                issue("validation", "引文不在当前原文块中或定位不符", [evidence.evidence_id])
            )
    valid_evidence = {e.evidence_id for e in result.evidence}
    for feature in candidate.features:
        if set(feature.evidence_ids) <= valid_evidence:
            result.features.append(feature)
        else:
            result.issues.append(issue("validation", "功能引用了无效证据", [feature.feature_id]))
    valid_features = {f.feature_id for f in result.features}
    evidence_by_id = {e.evidence_id: e for e in result.evidence}
    metadata_rule_ids: set[str] = set()
    for rule in candidate.rules:
        if rule.feature_id in valid_features and set(rule.evidence_ids) <= valid_evidence:
            if _is_metadata_rule(rule, evidence_by_id, block_map):
                metadata_rule_ids.add(rule.rule_id)
                result.issues.append(
                    issue(
                        "gap",
                        "文档标题/章节标题不是可执行需求，已从规则集移除",
                        [rule.feature_id],
                        severity="info",
                    )
                )
                continue
            # 词面引文通过仍不能证明模型的规则解释正确，默认保留人工复核状态。
            checked = rule.model_copy(deep=True)
            checked.status = (
                rule.status if rule.status in {"ambiguous", "conflicting"} else "needs_review"
            )
            constraint = checked.structured_constraint
            quote = "\n".join(
                e.quote for e in result.evidence if e.evidence_id in rule.evidence_ids
            )
            derived = False
            if rule.kind == "field_constraint" and (
                constraint is None or constraint.boundary is None
            ):
                inferred = derive_constraint(quote)
                if inferred is not None:
                    checked.structured_constraint = inferred
                    constraint = inferred
                    derived = True
                    result.issues.append(
                        issue(
                            "other",
                            "程序从原文补充数值约束",
                            [rule.rule_id],
                            severity="info",
                        )
                    )
            if constraint is not None:
                if constraint.boundary is not None and str(constraint.boundary) not in quote:
                    constraint.boundary = None
                    result.issues.append(
                        issue("gap", "结构化边界数值在引文中不存在", [rule.rule_id])
                    )
                if constraint.unit and constraint.unit not in quote:
                    constraint.unit = None
                    result.issues.append(issue("gap", "结构化单位缺少引文依据", [rule.rule_id]))
                required_like = any(
                    word in rule.statement
                    for word in (
                        "必填",
                        "必选",
                        "不能为空",
                        "必须填写",
                        "不可为空",
                        "不能缺失",
                    )
                )
                if (
                    constraint.boundary is not None
                    and required_like
                    and not re.search(r"\d", rule.statement)
                ):
                    constraint.boundary = None
                    constraint.unit = None
                    constraint.precision = None
                    result.issues.append(
                        issue(
                            "gap",
                            "必填规则没有数值依据，已移除跨句边界",
                            [rule.rule_id],
                            severity="info",
                        )
                    )
                if constraint.precision is not None and not derived:
                    reliable = (
                        bool(re.search(r"整数|[个条项字符]", quote))
                        if constraint.precision == 0
                        else bool(re.search(rf"小数\s*{constraint.precision}\s*位", quote))
                    )
                    if not reliable:
                        constraint.precision = None
                        result.issues.append(
                            issue("gap", "数值精度缺少明确依据，需人工确认", [rule.rule_id])
                        )
            result.rules.append(checked)
        else:
            result.issues.append(issue("validation", "规则的功能/证据引用无效", [rule.rule_id]))
    if metadata_rule_ids:
        result.issues = [
            item.model_copy(
                update={
                    "object_ids": [
                        object_id
                        for object_id in item.object_ids
                        if object_id not in metadata_rule_ids
                    ]
                }
            )
            for item in result.issues
        ]
    result.entities = [e for e in candidate.entities if set(e.evidence_ids) <= valid_evidence]
    return result


def error_rule_ids(issues: list[Issue], rules: list[RequirementRule]) -> set[str]:
    """模型以 error 明确判定无法提炼的规则不再进入测试点规划。"""
    known = {rule.rule_id for rule in rules}
    return {
        object_id
        for item in issues
        if item.severity == "error"
        for object_id in item.object_ids
        if object_id in known
    }


_NEGATION_WORDS = ("不", "禁止", "不得", "不可", "无需", "仅", "只")


def _negation_profile(text: str) -> frozenset[str]:
    return frozenset(word for word in _NEGATION_WORDS if word in text)


def _constraint_key(rule: RequirementRule) -> str | None:
    constraint = rule.structured_constraint
    if constraint is None:
        return None
    data = constraint.model_dump(mode="json")
    data["operator"] = _normalized_operator(constraint.operator)
    boundary = data.get("boundary")
    if isinstance(boundary, str):
        try:
            data["boundary"] = format(Decimal(boundary), "f")
        except (InvalidOperation, ValueError):
            pass
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _find_near_duplicate(
    rule: RequirementRule, bucket: list[RequirementRule]
) -> RequirementRule | None:
    """同功能、同类型、同约束下，语句完全或高度相似才视为同一规则。"""
    normalized = _normalized_statement(rule.statement)
    profile = _negation_profile(rule.statement)
    for candidate in bucket:
        other = _normalized_statement(candidate.statement)
        if other == normalized:
            return candidate
        if profile != _negation_profile(candidate.statement):
            continue
        if abs(len(other) - len(normalized)) > 8:
            continue
        if SequenceMatcher(None, other, normalized).ratio() >= 0.90:
            return candidate
    return None


_RULE_STATUS_ORDER = {"supported": 0, "needs_review": 1, "ambiguous": 2, "conflicting": 3}


RuleStatus = Literal["supported", "needs_review", "ambiguous", "conflicting"]


def _worse_rule_status(left: RuleStatus, right: RuleStatus) -> RuleStatus:
    return left if _RULE_STATUS_ORDER[left] >= _RULE_STATUS_ORDER[right] else right


def reconcile(extractions: list[Extraction]) -> Extraction:
    """只合并同功能、同原文块范围、同类型、同结构约束的精确重复规则。"""
    merged = Extraction()
    buckets: dict[tuple[str, str, str | None], list[RequirementRule]] = {}
    evidence_map: dict[str, EvidenceSpan] = {}
    features: dict[str, Feature] = {}
    feature_keys: dict[str, str] = {}
    feature_mapping: dict[str, str] = {}
    for extraction in extractions:
        merged.issues.extend(extraction.issues)
        evidence_map.update({e.evidence_id: e for e in extraction.evidence})
        for feature in extraction.features:
            scope = sorted({evidence_map[e].block_id for e in feature.evidence_ids})
            key = fingerprint([normalized_quote(feature.name), feature.description, scope])
            canonical = feature_keys.setdefault(key, feature.feature_id)
            feature_mapping[feature.feature_id] = canonical
            if canonical not in features:
                features[canonical] = feature.model_copy(deep=True)
            else:
                features[canonical].evidence_ids = sorted(
                    set(features[canonical].evidence_ids + feature.evidence_ids)
                )
        merged.entities.extend(extraction.entities)
        for rule in extraction.rules:
            rule = rule.model_copy(
                update={"feature_id": feature_mapping[rule.feature_id]}, deep=True
            )
            bucket_key = (rule.feature_id, rule.kind, _constraint_key(rule))
            bucket = buckets.setdefault(bucket_key, [])
            known = _find_near_duplicate(rule, bucket)
            if known is None:
                bucket.append(rule)
                known = rule
            else:
                known.evidence_ids = sorted(set(known.evidence_ids + rule.evidence_ids))
                known.status = _worse_rule_status(known.status, rule.status)
            merged.rule_mapping[rule.rule_id] = known.rule_id
    merged.evidence = list(evidence_map.values())
    merged.features = list(features.values())
    merged.rules = [rule for bucket in buckets.values() for rule in bucket]
    # 不猜测不同条件是否等价；同字段出现不同约束时显式提示，而非覆盖前一条。
    by_field: dict[tuple[str, str, str | None, str | None], list[RequirementRule]] = {}
    for rule in merged.rules:
        constraint = rule.structured_constraint
        if constraint and constraint.field_path:
            by_field.setdefault(
                (
                    rule.feature_id,
                    constraint.field_path,
                    constraint.applicable_when,
                    constraint.operator,
                ),
                [],
            ).append(rule)
    for rules in by_field.values():
        if len({fingerprint(r.structured_constraint) for r in rules}) > 1:
            merged.issues.append(
                issue("conflict", "同字段存在不同约束，需核对适用条件", [r.rule_id for r in rules])
            )
            for rule in rules:
                rule.status = "conflicting"
    return merged
