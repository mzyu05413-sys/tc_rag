"""问题生命周期决定运行状态；提示和已解决问题不伪装成失败。"""

from tc_rag.application.orchestration.pipeline import _final_status
from tc_rag.application.services.design import dedupe_issues, issue
from tc_rag.domain import models as m


def _item(severity: str, status: str = "open") -> m.Issue:
    return m.Issue(
        issue_id=f"i-{severity}-{status}",
        kind="gap",
        severity=severity,
        description="fixture",
        status=status,
    )


def test_only_open_warning_or_error_fails_run():
    assert _final_status([]) == "succeeded"
    assert _final_status([_item("info")]) == "succeeded"
    assert _final_status([_item("warning", "resolved")]) == "succeeded"
    assert _final_status([_item("error", "ignored")]) == "succeeded"
    assert _final_status([_item("warning")]) == "partial_failed"
    assert _final_status([_item("error")]) == "partial_failed"


def test_duplicate_issue_is_closed_by_merge():
    first = issue("coverage", "同一问题", ["p1"])
    second = issue("coverage", "同一问题", ["p2"])
    merged = dedupe_issues([first, second])
    assert len(merged) == 1
    assert merged[0].status == "resolved"
    assert merged[0].resolution == "deduplicated"
    assert merged[0].object_ids == ["p1", "p2"]
