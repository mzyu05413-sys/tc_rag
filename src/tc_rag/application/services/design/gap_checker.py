"""遗漏规则缺口核查（A3 报告先行）：报告检索候选里未被任何规则引用的证据。

“细规则漏提炼”与“检索漏召回”是两回事：本函数只报告“已检索到、但没有规则引用”
的证据，作为可能遗漏独立规则的候选，供人工核对；不自动把补充文字当新规则、不
改写规则集。检索接线（用功能范围做有界查询）由 pipeline 在后续步骤按预算接入。
"""

from collections.abc import Sequence

from tc_rag.application.services.design._common import issue
from tc_rag.domain.models import EvidenceSpan, Issue, RequirementRule


def uncovered_evidence(
    rules: Sequence[RequirementRule],
    candidates: Sequence[EvidenceSpan],
) -> list[EvidenceSpan]:
    """返回候选里未被任何规则引用的证据，作为遗漏规则的缺口候选。"""
    referenced = {evidence_id for rule in rules for evidence_id in rule.evidence_ids}
    return [item for item in candidates if item.evidence_id not in referenced]


def gap_report(
    rules: Sequence[RequirementRule],
    candidates: Sequence[EvidenceSpan],
) -> list[Issue]:
    """报告检索到但未被任何规则覆盖的证据；只报告、不写入规则集。"""
    uncovered = uncovered_evidence(rules, candidates)
    if not uncovered:
        return []
    return [
        issue(
            "gap",
            "检索到未被任何规则引用的证据，可能存在遗漏的独立规则，需人工核对",
            [item.evidence_id for item in uncovered],
            severity="info",
        )
    ]
