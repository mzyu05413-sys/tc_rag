"""遗漏规则缺口核查（A3 报告先行）：报告未被规则引用的检索证据。"""

from tc_rag.application.services.design.gap_checker import gap_report, uncovered_evidence
from tc_rag.core.config import Planning
from tc_rag.domain import models as m


def rule(rule_id="r1", evidence_ids=("e1",)):
    return m.RequirementRule(
        rule_id=rule_id, feature_id="f", kind="other",
        statement="规则陈述", evidence_ids=list(evidence_ids),
    )


def evidence(evidence_id):
    return m.EvidenceSpan(
        evidence_id=evidence_id, block_id="b", locator=m.Locator(page=1), quote=evidence_id,
    )


def test_uncovered_evidence_excludes_referenced():
    result = uncovered_evidence(
        [rule(evidence_ids=["e1"])], [evidence("e1"), evidence("e2")]
    )
    assert [e.evidence_id for e in result] == ["e2"]


def test_gap_report_emits_candidate_when_uncovered():
    issues = gap_report([rule(evidence_ids=["e1"])], [evidence("e2")])
    assert len(issues) == 1
    assert issues[0].kind == "gap" and issues[0].severity == "info"
    assert issues[0].object_ids == ["e2"]


def test_gap_report_empty_when_all_covered():
    assert gap_report([rule(evidence_ids=["e1"])], [evidence("e1")]) == []


def test_planning_config_gap_check_defaults_off_and_hidden():
    assert Planning().gap_check_rounds == 0
    assert "gap_check_rounds" not in Planning().model_dump(mode="json")
    assert Planning(gap_check_rounds=2).model_dump(mode="json")["gap_check_rounds"] == 2
