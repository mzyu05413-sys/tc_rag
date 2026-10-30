"""证据核验、保守归并与确定性测试设计；无法确定的语义留待人工。"""

import json
import math
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.models import (
    CoverageLink,
    DocumentBlock,
    EvidenceSpan,
    Feature,
    Issue,
    RequirementRule,
    TestCase,
    TestPoint,
)
from tc_rag.domain.pipeline import Extraction


def issue(kind: str, description: str, ids: list[str] | None = None) -> Issue:
    return Issue.model_validate(
        dict(
            issue_id=new_id("issue"),
            kind=kind,
            severity="warning",
            description=description,
            object_ids=ids or [],
        )
    )


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
    for rule in candidate.rules:
        if rule.feature_id in valid_features and set(rule.evidence_ids) <= valid_evidence:
            # 词面引文通过仍不能证明模型的规则解释正确，默认保留人工复核状态。
            checked = rule.model_copy(deep=True)
            checked.status = (
                rule.status if rule.status in {"ambiguous", "conflicting"} else "needs_review"
            )
            constraint = checked.structured_constraint
            quote = "\n".join(
                e.quote for e in result.evidence if e.evidence_id in rule.evidence_ids
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
                if constraint.precision is not None:
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
    result.entities = [e for e in candidate.entities if set(e.evidence_ids) <= valid_evidence]
    return result


def reconcile(extractions: list[Extraction]) -> Extraction:
    """只合并同功能、同原文块范围、同类型、同结构约束的精确重复规则。"""
    merged = Extraction()
    keys: dict[str, RequirementRule] = {}
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
            scope = sorted({evidence_map[e].block_id for e in rule.evidence_ids})
            key = fingerprint(
                [
                    rule.feature_id,
                    scope,
                    rule.kind,
                    normalized_quote(rule.statement),
                    rule.structured_constraint.model_dump() if rule.structured_constraint else None,
                ]
            )
            if key in keys:
                previous = keys[key]
                previous.evidence_ids = sorted(set(previous.evidence_ids + rule.evidence_ids))
            else:
                keys[key] = rule
            merged.rule_mapping[rule.rule_id] = keys[key].rule_id
    merged.evidence = list(evidence_map.values())
    merged.features = list(features.values())
    merged.rules = list(keys.values())
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


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """纯 Python 余弦相似度；为空或零向量时视为不可比（返回 -1）。"""
    if len(a) != len(b) or not a:
        return -1.0
    dot: float = 0.0
    for x, y in zip(a, b):
        dot += x * y
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def _constraint_key(rule: RequirementRule) -> str | None:
    """结构化约束严格规范化后作为分组键；None 与无约束区分开。"""
    c = rule.structured_constraint
    if c is None:
        return None
    return json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def deduplicate_semantic(
    rules: list[RequirementRule],
    vectors: dict[str, list[float]],
    threshold: float = 0.92,
) -> Extraction:
    """严格保守的语义重复合并。

    仅在同功能、同规则类型、同结构化约束的规则之间比较 statement 向量相似度，
    达到阈值才保留一条并合并证据并集；绝不合并状态为 ambiguous/conflicting 的
    规则，也不跨功能或类型合并。输入向量缺失的规则不被合并，保留原样。
    返回去重后的 Extraction，rule_mapping 记录被合并规则到保留规则的映射。
    """
    merged = Extraction()
    # 分组：仅同键内才有合并可能，避免把不同适用条件的功能误判为等价。
    groups: dict[tuple[str, str, str | None], list[RequirementRule]] = {}
    for rule in rules:
        key = (rule.feature_id, rule.kind, _constraint_key(rule))
        groups.setdefault(key, []).append(rule)

    final_rules: list[RequirementRule] = []
    mapping: dict[str, str] = {}
    for group in groups.values():
        stable = sorted(group, key=lambda r: r.rule_id)
        # 语义不确定或约束冲突的规则不参与合并，也不作为其他规则的合并目标。
        excluded = {r.rule_id for r in stable if r.status in {"ambiguous", "conflicting"}}
        representatives: list[RequirementRule] = []
        for rule in stable:
            if rule.rule_id in excluded:
                representatives.append(rule)
                mapping[rule.rule_id] = rule.rule_id
                continue
            vector = vectors.get(rule.rule_id)
            if vector is None:
                representatives.append(rule)
                mapping[rule.rule_id] = rule.rule_id
                continue
            merged_into: RequirementRule | None = None
            for rep in representatives:
                if rep.rule_id in excluded:
                    continue
                rep_vector = vectors.get(rep.rule_id)
                if rep_vector is None:
                    continue
                if _cosine(vector, rep_vector) >= threshold:
                    merged_into = rep
                    break
            if merged_into is None:
                representatives.append(rule)
                mapping[rule.rule_id] = rule.rule_id
            else:
                combined = sorted(set(merged_into.evidence_ids) | set(rule.evidence_ids))
                idx = representatives.index(merged_into)
                representatives[idx] = merged_into.model_copy(
                    update={"evidence_ids": combined}, deep=True
                )
                mapping[rule.rule_id] = merged_into.rule_id
        final_rules.extend(representatives)

    merged.rules = final_rules
    merged.rule_mapping = mapping
    # 证据、功能、实体保持原样：语义合并只变更规则，不删除仍可能被引用的证据。
    return merged


_OPERATOR_ALIASES = {
    "lt": "<",
    "le": "<=",
    "leq": "<=",
    "gt": ">",
    "ge": ">=",
    "geq": ">=",
    "eq": "==",
    "≤": "<=",
    "≥": ">=",
    "＜": "<",
    "＞": ">",
    "＝": "==",
    "max_length": "<=",
    "min_length": ">=",
    "不大于": "<=",
    "不超过": "<=",
    "至多": "<=",
    "最多": "<=",
    "不小于": ">=",
    "不低于": ">=",
    "至少": ">=",
    "大于": ">",
    "小于": "<",
    "等于": "==",
}
_COMPARISONS = {"<", "<=", ">", ">=", "=="}


def _normalized_operator(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return _OPERATOR_ALIASES.get(text) or _OPERATOR_ALIASES.get(text.lower()) or text


def boundary_values(rule: RequirementRule) -> list[str]:
    """精度明确时生成边界两侧及边界本身；300、整数精度0 → 299/300/301。"""
    c = rule.structured_constraint
    if c is None or c.boundary is None:
        return []
    operator = _normalized_operator(c.operator)
    if operator not in _COMPARISONS:
        return []
    precision = c.precision
    if precision is None:
        # 长度类约束的精度天然是整数，可确定步长；其他缺精度时保守返回空。
        if (c.operator or "").strip().lower() in {"max_length", "min_length"}:
            precision = 0
        else:
            return []
    try:
        value = Decimal(str(c.boundary))
        step = Decimal(1).scaleb(-precision)
        if not value.is_finite() or value.quantize(step) != value:
            return []
        return [format(v, "f") for v in (value - step, value, value + step)]
    except (InvalidOperation, ValueError):
        return []


def plan_points(rules: list[RequirementRule], methods: Sequence[str]) -> list[TestPoint]:
    """按规则种类选择适用方法；不强制为所有规则凑齐所有设计方法。"""
    mapping = {
        "field_constraint": ["equivalence", "boundary"],
        "business_condition": ["decision_table"],
        "permission": ["permission"],
        "state_transition": ["state"],
        "workflow": ["workflow"],
        "error_behavior": ["error_behavior"],
        "other": ["other"],
    }
    points: list[TestPoint] = []
    for rule in rules:
        for method in mapping[rule.kind]:
            if method not in methods and method not in {"workflow", "error_behavior", "other"}:
                continue
            values = boundary_values(rule) if method == "boundary" else []
            objectives = (
                [f"{rule.statement}；边界输入={v}" for v in values]
                if values
                else [rule.statement]
            )
            for objective in objectives:
                undetermined = (method == "boundary" and not values) or rule.status in {
                    "conflicting",
                    "ambiguous",
                }
                points.append(
                    TestPoint.model_validate(
                        dict(
                            point_id=new_id("point"),
                            rule_ids=[rule.rule_id],
                            technique=method,
                            objective=objective,
                            applicability="undetermined" if undetermined else "applicable",
                            status="needs_clarification" if undetermined else "ready",
                        )
                    )
                )
    return points


def validate_cases(
    cases: list[TestCase],
    points: list[TestPoint],
    rules: list[RequirementRule],
    evidence: list[EvidenceSpan],
) -> tuple[list[TestCase], list[CoverageLink], list[Issue]]:
    """校验当前运行引用闭包、步骤覆盖和词面风险；语义正确性仍需人工判断。"""
    pm, rm, em = (
        {p.point_id: p for p in points},
        {r.rule_id: r for r in rules},
        {e.evidence_id: e for e in evidence},
    )
    output: list[TestCase] = []
    links: list[CoverageLink] = []
    issues: list[Issue] = []
    signatures: set[str] = set()
    for case in cases:
        if not set(case.point_ids) <= pm.keys():
            issues.append(issue("validation", "用例引用了当前批次之外的测试点", [case.case_id]))
            continue
        allowed_rules = {r for p in case.point_ids for r in pm[p].rule_ids}
        if any(not step.action.strip() or not step.expected.strip() for step in case.steps):
            issues.append(issue("validation", "用例动作或预期只有空白，拒绝该用例", [case.case_id]))
            continue
        if len(case.point_ids) > 1 and any(pm[p].technique == "boundary" for p in case.point_ids):
            issues.append(
                issue("validation", "边界测试点必须分别生成用例，不能合并", [case.case_id])
            )
            continue
        if any(
            not set(s.rule_ids) <= allowed_rules
            or not set(s.rule_ids) <= rm.keys()
            or not set(s.evidence_ids) <= em.keys()
            for s in case.steps
        ):
            issues.append(issue("validation", "步骤规则/证据引用无效，拒绝该用例", [case.case_id]))
            continue
        flagged = False
        signature = fingerprint(
            [case.preconditions, case.data, [[s.action, s.expected] for s in case.steps]]
        )
        if signature in signatures:
            issues.append(issue("duplicate", "步骤及输入完全重复，保留并标记", [case.case_id]))
            flagged = True
        signatures.add(signature)
        for number, step in enumerate(case.steps, 1):
            expected_evidence = {e for r in step.rule_ids for e in rm[r].evidence_ids}
            quote = "\n".join(em[e].quote for e in step.evidence_ids)
            # 只对预期断言检查数值，不把299/301等合法边界测试输入误认为需求事实。
            numbers = re.findall(r"(?<!\w)\d+(?:\.\d+)?", step.expected)
            if not set(step.evidence_ids) <= expected_evidence or any(
                n not in quote for n in numbers
            ):
                issues.append(
                    issue(
                        "unsupported_expectation", "预期数值或证据范围缺乏直接依据", [case.case_id]
                    )
                )
                flagged = True
            if normalized_quote(step.expected) not in normalized_quote(quote):
                flagged = True
                issues.append(
                    issue(
                        "unsupported_expectation",
                        "预期为解释性断言，需人工核对语义",
                        [case.case_id],
                    )
                )
            for rule_id in step.rule_ids:
                for point_id in case.point_ids:
                    if rule_id in pm[point_id].rule_ids:
                        links.append(
                            CoverageLink(
                                case_id=case.case_id,
                                step_no=number,
                                rule_id=rule_id,
                                point_id=point_id,
                                validation_status="needs_review",
                            )
                        )
        # 自动检查始终不替代人工批准；此状态只描述技术检查结果。
        output.append(
            case.model_copy(update={"status": "needs_review" if flagged else "validated"})
        )
    covered = {link.point_id for link in links}
    for point in points:
        if point.point_id not in covered:
            issues.append(issue("coverage", "测试点没有用例覆盖", [point.point_id]))
    return output, links, issues
