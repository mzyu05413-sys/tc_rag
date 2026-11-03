"""依据明确规则计算边界并确定性规划测试点。"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from tc_rag.application.services.design._common import (
    _SIZE_UNITS,
    _TIME_UNITS,
    _normalized_operator,
)
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import RequirementRule, TestPoint

_COMPARISONS = {"<", "<=", ">", ">=", "=="}


def boundary_values(rule: RequirementRule) -> list[str]:
    """精度明确时生成边界两侧及边界本身；300、整数精度0 → 299/300/301。"""
    c = rule.structured_constraint
    if c is None or c.boundary is None:
        return []
    unit = (c.unit or "").strip()
    if unit.upper() in _SIZE_UNITS or unit in _TIME_UNITS:
        # 尺寸/时长没有可推断的最小步长，改由定性边界描述，不编造小数。
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


def qualitative_boundary_values(rule: RequirementRule) -> list[str]:
    """尺寸/时长类约束用定性边界描述，避免编造原文没有的精度。"""
    c = rule.structured_constraint
    if c is None or c.boundary is None:
        return []
    unit = (c.unit or "").strip()
    if unit.upper() in _SIZE_UNITS:
        label = f"{c.boundary}{unit.upper()}"
    elif unit in _TIME_UNITS:
        label = f"{c.boundary}{unit}"
    else:
        return []
    return [f"略低于{label}", f"等于{label}", f"略高于{label}"]


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
        boundary_values_for_rule = (
            boundary_values(rule) or qualitative_boundary_values(rule)
            if rule.kind == "field_constraint"
            else []
        )
        for method in mapping[rule.kind]:
            if method not in methods and method not in {"workflow", "error_behavior", "other"}:
                continue
            if method == "equivalence" and boundary_values_for_rule:
                # 数值/长度边界点已携带完整数据，不再另造无数据的等价类点。
                continue
            values = boundary_values_for_rule if method == "boundary" else []
            if method == "boundary" and not values:
                continue
            objective = (
                f"{rule.statement}；边界数据={'/'.join(values)}"
                if values
                else rule.statement
            )
            undetermined = rule.status in {"conflicting", "ambiguous"}
            points.append(
                TestPoint.model_validate(
                    dict(
                        point_id=new_id("point"),
                        rule_ids=[rule.rule_id],
                        technique=method,
                        objective=objective,
                        data_variants=values,
                        applicability="undetermined" if undetermined else "applicable",
                        status="needs_clarification" if undetermined else "ready",
                    )
                )
            )
    return points
