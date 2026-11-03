"""质检共享的结果构造与快照访问；不判断业务语义。"""

from collections.abc import Callable, Mapping
from typing import Any

from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint

Check = dict[str, Any]


SnapshotMap = Mapping[str, dict[str, Any]]


_FAIL = "fail"


_PASS = "pass"


_WARN = "warn"


_NA = "not_applicable"


def _check(
    check_id: str,
    stage: str,
    severity: str,
    status: str,
    summary: str,
    *,
    counts: dict[str, int] | None = None,
    object_ids: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> Check:
    return {
        "check_id": check_id,
        "stage": stage,
        "severity": severity,
        "status": status,
        "summary": summary,
        "counts": counts or {},
        "object_ids": (object_ids or [])[:50],
        "detail": detail or {},
    }


def _pass(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "info", _PASS, summary, **kwargs)


def _warn(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "warning", _WARN, summary, **kwargs)


def _fail(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "error", _FAIL, summary, **kwargs)


def _na(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "info", _NA, summary, **kwargs)


def _safe(
    check_id: str,
    stage: str,
    schema_errors: dict[str, str],
    operation: Callable[[], Check],
) -> Check:
    if stage in schema_errors:
        return _na(
            check_id,
            stage,
            "阶段快照结构无效，跳过该检查",
            detail={"schema_error": schema_errors[stage]},
        )
    try:
        return operation()
    except Exception as exc:
        return _fail(
            check_id,
            stage,
            "检查执行失败",
            detail={"error_type": type(exc).__name__},
        )


def _blocks(chunk: dict[str, Any] | None) -> list[m.DocumentBlock]:
    if not chunk:
        return []
    return [m.DocumentBlock.model_validate(item) for item in chunk.get("blocks", [])]


def _case_point(item: dict[str, Any]) -> m.TestPoint:
    # cases 快照的 point 额外带导出用 category 字段；它不是领域契约的一部分。
    return m.TestPoint.model_validate(
        {key: value for key, value in item.items() if key != "category"}
    )


def _issues(snapshots: SnapshotMap) -> list[dict[str, Any]]:
    issue_snapshot = snapshots.get("issues")
    if issue_snapshot is not None:
        return list(issue_snapshot.get("items", []))
    cases = snapshots.get("cases")
    return [] if cases is None else list(cases.get("issues", []))


def _evidence_by_id(extract: dict[str, Any] | None) -> dict[str, m.EvidenceSpan]:
    if not extract:
        return {}
    return {
        item.evidence_id: item
        for item in (m.EvidenceSpan.model_validate(e) for e in extract.get("evidence", []))
    }


def _rules_by_id(extract: dict[str, Any] | None) -> dict[str, m.RequirementRule]:
    if not extract:
        return {}
    return {
        item.rule_id: item
        for item in (m.RequirementRule.model_validate(r) for r in extract.get("rules", []))
    }


def _source_evidence_ids(chunk: dict[str, Any] | None) -> set[str]:
    if not chunk:
        return set()
    result: set[str] = set()
    for item in chunk.get("chunks", []):
        for source in item.get("sources", []):
            model = m.ChunkSource.model_validate(source)
            result.add("source_" + fingerprint(model.model_dump(mode="json")))
    return result


def _contains_api_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "api_key" and item not in (None, ""):
                return True
            if _contains_api_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_api_key(item) for item in value)
    return False


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
check = _check
passed = _pass
warn = _warn
fail = _fail
not_applicable = _na
safe = _safe
blocks = _blocks
case_point = _case_point
issues = _issues
evidence_by_id = _evidence_by_id
rules_by_id = _rules_by_id
source_evidence_ids = _source_evidence_ids
contains_api_key = _contains_api_key
