"""按既有契约归并重复测试点与问题。"""

from difflib import SequenceMatcher

from tc_rag.application.services.design._common import _normalized_statement, issue
from tc_rag.domain.models import Issue, TestPoint

_TECHNIQUE_ORDER = (
    "boundary",
    "decision_table",
    "state",
    "permission",
    "workflow",
    "error_behavior",
    "equivalence",
    "other",
)


_STATUS_ORDER = {"ready": 0, "needs_context": 1, "needs_clarification": 2, "not_applicable": 3}


def _technique_rank(point: TestPoint) -> int:
    try:
        return _TECHNIQUE_ORDER.index(point.technique)
    except ValueError:
        return len(_TECHNIQUE_ORDER)


def _applicability_for(status: str) -> str:
    if status == "ready":
        return "applicable"
    if status == "not_applicable":
        return "not_applicable"
    return "undetermined"


def deduplicate_points(points: list[TestPoint]) -> tuple[list[TestPoint], list[Issue]]:
    """同一规则集合下的相同或近义目标只保留一条，优先保留更具体的测试方法。"""
    kept: list[TestPoint] = []
    merged_ids: dict[str, list[str]] = {}
    for point in points:
        normalized = _normalized_statement(point.objective)
        match_index: int | None = None
        for index, candidate in enumerate(kept):
            if not set(candidate.rule_ids) & set(point.rule_ids):
                continue
            other = _normalized_statement(candidate.objective)
            if other == normalized or (
                candidate.technique != point.technique
                and SequenceMatcher(None, other, normalized).ratio() >= 0.90
            ):
                match_index = index
                break
        if match_index is None:
            kept.append(point)
            continue
        candidate = kept[match_index]
        survivor = (
            candidate if _technique_rank(candidate) <= _technique_rank(point) else point
        )
        dropped = point if survivor is candidate else candidate
        status = min(
            (candidate.status, point.status), key=lambda item: _STATUS_ORDER.get(item, 1)
        )
        kept[match_index] = survivor.model_copy(
            update={
                "rule_ids": sorted(set(candidate.rule_ids) | set(point.rule_ids)),
                "status": status,
                "applicability": _applicability_for(status),
            }
        )
        merged_ids.setdefault(survivor.point_id, []).append(dropped.point_id)
    issues = [
        issue(
            "duplicate",
            f"合并 {len(dropped) + 1} 个重复或近义测试目标",
            [point_id, *dropped],
            severity="info",
        )
        for point_id, dropped in merged_ids.items()
    ]
    return kept, issues


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """同类型、同严重度、同描述的问题合并对象 ID，保留首次出现顺序。"""
    merged: dict[tuple[str, str, str], Issue] = {}
    order: list[tuple[str, str, str]] = []
    for item in issues:
        key = (item.kind, item.severity, _normalized_statement(item.description))
        if key in merged:
            previous = merged[key]
            merged[key] = previous.model_copy(
                update={
                    "object_ids": sorted(set(previous.object_ids) | set(item.object_ids)),
                    "status": "resolved",
                    "resolution": "deduplicated",
                }
            )
        else:
            merged[key] = item
            order.append(key)
    return [merged[key] for key in order]
