"""用例引用、依据风险、重复和覆盖检查。"""

from collections import Counter

from tc_rag.application.services.design import case_signature, unsupported_numbers
from tc_rag.application.services.quality._common import Check, SnapshotMap
from tc_rag.application.services.quality._common import case_point as _case_point
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn
from tc_rag.domain import models as m


def _case_checks(
    snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    cases_payload = snapshots.get("cases")

    def point_closure() -> Check:
        if cases_payload is None:
            return _na("Q-CS-01", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        bad: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if not case.point_ids or not set(case.point_ids) <= points.keys():
                bad.append(case.case_id)
        if bad:
            return _fail(
                "Q-CS-01",
                "cases",
                "用例引用了不存在的测试点",
                counts={"cases": len(cases_payload.get("cases", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-CS-01",
            "cases",
            "用例测试点引用闭包完整",
            counts={"cases": len(cases_payload.get("cases", []))},
        )

    def step_closure() -> Check:
        if cases_payload is None:
            return _na("Q-CS-02", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id")
            for item in cases_payload.get("evidence", [])
        }
        bad: list[str] = []
        boundary_mixed: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            allowed = {
                rule_id
                for point_id in case.point_ids
                if point_id in points
                for rule_id in points[point_id].rule_ids
            }
            if len(case.point_ids) > 1 and any(
                point_id in points and points[point_id].technique == "boundary"
                for point_id in case.point_ids
            ):
                boundary_mixed.append(case.case_id)
            invalid = any(
                not step.action.strip()
                or not step.expected.strip()
                or not set(step.rule_ids) <= allowed
                or not set(step.rule_ids) <= set(rules)
                or not set(step.evidence_ids) <= evidence
                for step in case.steps
            )
            if invalid:
                bad.append(case.case_id)
        if bad or boundary_mixed:
            return _fail(
                "Q-CS-02",
                "cases",
                "用例步骤引用越界、动作为空或边界点被合并",
                counts={"invalid": len(bad), "boundary_mixed": len(boundary_mixed)},
                object_ids=sorted(set(bad + boundary_mixed)),
            )
        return _pass("Q-CS-02", "cases", "用例步骤引用闭包完整")

    def coverage_integrity() -> Check:
        if cases_payload is None:
            return _na("Q-CS-03", "cases", "缺少 cases 快照")
        cases = {
            item.get("case_id"): m.TestCase.model_validate(item)
            for item in cases_payload.get("cases", [])
        }
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {rule.get("rule_id") for rule in cases_payload.get("rules", [])}
        bad: list[str] = []
        duplicates: set[tuple[str, int, str, str]] = set()
        for item in cases_payload.get("coverage", []):
            link = m.CoverageLink.model_validate(item)
            key = (link.case_id, link.step_no, link.rule_id, link.point_id)
            case = cases.get(link.case_id)
            point = points.get(link.point_id)
            if (
                case is None
                or point is None
                or link.rule_id not in rules
                or link.rule_id not in point.rule_ids
                or not 1 <= link.step_no <= len(case.steps)
            ):
                bad.append(link.case_id)
            if key in duplicates:
                bad.append(link.case_id)
            duplicates.add(key)
        if bad:
            return _fail(
                "Q-CS-03",
                "cases",
                "覆盖关联引用无效或重复",
                counts={"links": len(cases_payload.get("coverage", [])), "invalid": len(set(bad))},
                object_ids=sorted(set(bad)),
            )
        return _pass(
            "Q-CS-03",
            "cases",
            "覆盖关联引用的用例/步骤/规则/测试点有效",
            counts={"links": len(cases_payload.get("coverage", []))},
        )

    def link_set() -> Check:
        if cases_payload is None:
            return _na("Q-CS-04", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id"): m.EvidenceSpan.model_validate(item)
            for item in cases_payload.get("evidence", [])
        }
        expected: set[tuple[str, int, str, str]] = set()
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if not set(case.point_ids) <= points.keys():
                continue
            if any(
                not set(step.rule_ids) <= set(rules)
                or not set(step.evidence_ids) <= set(evidence)
                for step in case.steps
            ):
                continue
            for number, step in enumerate(case.steps, 1):
                for rule_id in step.rule_ids:
                    for point_id in case.point_ids:
                        if rule_id in points[point_id].rule_ids:
                            expected.add((case.case_id, number, rule_id, point_id))
        saved = {
            (
                item.get("case_id", ""),
                int(item.get("step_no", 0)),
                item.get("rule_id", ""),
                item.get("point_id", ""),
            )
            for item in cases_payload.get("coverage", [])
        }
        missing = expected - saved
        extra = saved - expected
        if missing or extra:
            return _warn(
                "Q-CS-04",
                "cases",
                "已保存覆盖关联与重算集合存在差异",
                counts={"missing": len(missing), "extra": len(extra)},
                object_ids=sorted({item[0] for item in missing | extra}),
            )
        return _pass(
            "Q-CS-04",
            "cases",
            "覆盖关联集合与重算结果一致",
            counts={"links": len(expected)},
        )

    def duplicate_cases() -> Check:
        if cases_payload is None:
            return _na("Q-CS-05", "cases", "缺少 cases 快照")
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            signature = case_signature(case)
            if signature in seen:
                duplicates.append(case.case_id)
            seen.add(signature)
        if duplicates:
            return _warn(
                "Q-CS-05",
                "cases",
                "存在步骤和输入完全重复的用例",
                counts={"duplicates": len(duplicates)},
                object_ids=duplicates,
            )
        return _pass("Q-CS-05", "cases", "用例无完全重复")

    def unsupported_expectations() -> Check:
        if cases_payload is None:
            return _na("Q-CS-06", "cases", "缺少 cases 快照")
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id"): m.EvidenceSpan.model_validate(item)
            for item in cases_payload.get("evidence", [])
        }
        flagged: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if any(
                unsupported_numbers(step, rules, evidence) for step in case.steps
            ):
                flagged.append(case.case_id)
        if flagged:
            return _warn(
                "Q-CS-06",
                "cases",
                "用例预期含无直接依据的数字，需人工确认",
                counts={"cases": len(flagged)},
                object_ids=flagged,
            )
        return _pass("Q-CS-06", "cases", "未发现无依据的预期数字")

    def status_check() -> Check:
        if cases_payload is None:
            return _na("Q-CS-07", "cases", "缺少 cases 快照")
        statuses = Counter(
            str(item.get("status", "")) for item in cases_payload.get("cases", [])
        )
        counts = {key: value for key, value in statuses.items()}
        if statuses and set(statuses) == {"needs_review"}:
            return _warn(
                "Q-CS-07",
                "cases",
                "全部用例都被自动检查标记为 needs_review",
                counts=counts,
            )
        return _pass("Q-CS-07", "cases", "用例状态分布可用", counts=counts)

    def uncovered_points() -> Check:
        if cases_payload is None:
            return _na("Q-CS-08", "cases", "缺少 cases 快照")
        points = [
            _case_point(item)
            for item in cases_payload.get("points", [])
        ]
        covered = {
            point_id
            for item in cases_payload.get("cases", [])
            for point_id in item.get("point_ids", [])
        }
        missing = [
            point.point_id
            for point in points
            if point.status == "ready"
            and point.applicability == "applicable"
            and point.point_id not in covered
        ]
        if missing:
            return _warn(
                "Q-CS-08",
                "cases",
                "ready 且适用的测试点没有用例覆盖",
                counts={"missing": len(missing)},
                object_ids=missing,
            )
        return _pass("Q-CS-08", "cases", "ready 测试点均有用例覆盖")

    return [
        _safe("Q-CS-01", "cases", schema_errors, point_closure),
        _safe("Q-CS-02", "cases", schema_errors, step_closure),
        _safe("Q-CS-03", "cases", schema_errors, coverage_integrity),
        _safe("Q-CS-04", "cases", schema_errors, link_set),
        _safe("Q-CS-05", "cases", schema_errors, duplicate_cases),
        _safe("Q-CS-06", "cases", schema_errors, unsupported_expectations),
        _safe("Q-CS-07", "cases", schema_errors, status_check),
        _safe("Q-CS-08", "cases", schema_errors, uncovered_points),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
case_checks = _case_checks
