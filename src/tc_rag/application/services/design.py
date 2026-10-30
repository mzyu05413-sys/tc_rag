"""证据核验、保守归并与确定性测试设计；无法确定的语义留待人工。"""

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from tc_rag.application.services.chunking import normalized_quote, verify_evidence
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.models import (
    CoverageLink,
    DocumentBlock,
    EvidenceSpan,
    Feature,
    Issue,
    RequirementRule,
    StructuredConstraint,
    TestCase,
    TestPoint,
)
from tc_rag.domain.pipeline import Extraction


def issue(
    kind: str,
    description: str,
    ids: list[str] | None = None,
    severity: str = "warning",
) -> Issue:
    return Issue.model_validate(
        dict(
            issue_id=new_id("issue"),
            kind=kind,
            severity=severity,
            description=description,
            object_ids=ids or [],
        )
    )


_NUMBER = r"(\d+(?:\.\d+)?)"
# 只从证据原文提取，且要求同一段文本里只有一个明确的数值约束。
_CONSTRAINT_PATTERNS = (
    (rf"(?:最多|不超过|至多|不大于|上限(?:为|是)?|最大(?:值|数)?(?:为|是)?)\s*{_NUMBER}", "<="),
    (rf"(?:至少|不少于|不低于|下限(?:为|是)?|最小(?:值|数)?(?:为|是)?)\s*{_NUMBER}", ">="),
    (rf"(?:大于|超过|高于)\s*{_NUMBER}", ">"),
    (rf"(?:小于|低于|少于)\s*{_NUMBER}", "<"),
    (rf"长度(?:限制|上限)?\s*(?:为|是)?\s*{_NUMBER}", "<="),
    (rf"{_NUMBER}\s*(?:个)?字符", "<="),
    (rf"{_NUMBER}\s*(?:位|个|条|次|天|元)", "<="),
)


def derive_constraint(quote: str) -> StructuredConstraint | None:
    """从原文引文提取单一数值约束；多个不同数值视为冲突，不猜测。"""
    text = unicodedata.normalize("NFKC", quote)
    found: set[tuple[str, str]] = set()
    for pattern, operator in _CONSTRAINT_PATTERNS:
        for match in re.finditer(pattern, text):
            found.add((operator, match.group(1)))
    if len(found) != 1:
        return None
    operator, value = next(iter(found))
    precision = len(value.split(".", 1)[1]) if "." in value else 0
    unit = next(
        (
            candidate
            for candidate in ("字符", "位", "个", "条", "次", "天", "元")
            if candidate in text
        ),
        None,
    )
    boundary: Any = Decimal(value) if "." in value else int(value)
    return StructuredConstraint(
        operator=operator, boundary=boundary, precision=precision, unit=unit
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
            if method == "boundary":
                constraint = rule.structured_constraint
                # 必填/格式类约束没有数值边界，不生成无意义的边界测试点。
                if not values and (constraint is None or constraint.boundary is None):
                    continue
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
            # 只对预期断言检查数值；动作里出现的边界输入值不算无依据。
            number_pattern = r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?"
            expected_numbers = re.findall(number_pattern, step.expected)
            action_numbers = set(re.findall(number_pattern, step.action))
            # 属于该规则边界推导值(如 299/300/301)的不算无依据，它们是程序推的不是模型编的。
            # 需求文档只写"最多300"，模型预期写"输入301个提示错误"时不应误报。
            rule_boundaries: set[str] = set()
            for rule_id in step.rule_ids:
                rule = rm.get(rule_id)
                if rule is not None:
                    rule_boundaries.update(boundary_values(rule))
            unsupported_numbers = [
                n
                for n in expected_numbers
                if n not in quote and n not in action_numbers and n not in rule_boundaries
            ]
            if not set(step.evidence_ids) <= expected_evidence or unsupported_numbers:
                issues.append(
                    issue(
                        "unsupported_expectation", "预期数值或证据范围缺乏直接依据", [case.case_id]
                    )
                )
                flagged = True
            if normalized_quote(step.expected) not in normalized_quote(quote):
                # 业务化预期通常不会逐字出现在原文中，只作提示，不阻断自动校验状态。
                issues.append(
                    issue(
                        "unsupported_expectation",
                        "预期为业务表达，未逐字出现在原文中，建议人工确认",
                        [case.case_id],
                        severity="info",
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
