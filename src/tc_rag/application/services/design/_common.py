"""测试设计共享的问题构造、文本归一化、证据和单位常量。"""

import re
import unicodedata
from collections.abc import Mapping

from tc_rag.domain.identity import new_id
from tc_rag.domain.models import EvidenceSpan, Issue, RequirementRule


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


_SIZE_UNITS = {"MB", "GB", "KB"}


_TIME_UNITS = {"秒", "分钟", "小时"}


def _normalized_statement(text: str) -> str:
    """用于近义比较：统一全半角、去标点空白和常见模态词，保留否定词。"""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[\W_]+", "", value)
    for word in ("需要", "必须", "应当", "应该", "需", "须", "应"):
        value = value.replace(word, "")
    return value


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


def _normalized_operator(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return _OPERATOR_ALIASES.get(text) or _OPERATOR_ALIASES.get(text.lower()) or text


def _resolved_rule_evidence(
    rule_ids: list[str],
    rules: Mapping[str, RequirementRule],
    evidence: Mapping[str, EvidenceSpan],
) -> tuple[list[RequirementRule], list[EvidenceSpan]] | None:
    resolved_rules = [rules[rule_id] for rule_id in rule_ids if rule_id in rules]
    if len(resolved_rules) != len(rule_ids) or not resolved_rules:
        return None
    evidence_ids = {item for rule in resolved_rules for item in rule.evidence_ids}
    resolved_evidence = [evidence[item] for item in evidence_ids if item in evidence]
    if len(resolved_evidence) != len(evidence_ids) or not resolved_evidence:
        return None
    return resolved_rules, resolved_evidence
