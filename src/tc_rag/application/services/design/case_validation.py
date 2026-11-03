"""用例引用闭包、数值依据、精确重复及覆盖校验。"""

import re
import unicodedata
from typing import Any, Literal

from tc_rag.application.services.chunking import normalized_quote
from tc_rag.application.services.design._common import issue
from tc_rag.application.services.design.planning import boundary_values
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import (
    CoverageLink,
    EvidenceSpan,
    Issue,
    RequirementRule,
    TestCase,
    TestPoint,
    TestStep,
)


def _normalized_case_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def case_signature(case: TestCase) -> str:
    """步骤与输入归一化后相同的用例签名；只描述事实，不判断语义是否正确。"""
    return fingerprint(
        [
            [_normalized_case_text(item) for item in case.preconditions],
            case.data,
            [
                [_normalized_case_text(step.action), _normalized_case_text(step.expected)]
                for step in case.steps
            ],
        ]
    )


_NUMBER_PATTERN = r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?"


_TIME_PATTERN = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")


_CONTRAST_PATTERN = re.compile(r"(?:并非|而非|不是|不按)\s*(\d+(?:\.\d+)?)")


def _numeric_tokens(text: str) -> set[str]:
    """数字与时间统一成可比较的 token，避免 09:00 被拆成 9/00。"""
    times = {
        f"{int(hour):02d}:{minute}"
        for hour, minute in _TIME_PATTERN.findall(text)
    }
    cleaned = _TIME_PATTERN.sub(" ", text)
    numbers = {
        str(int(token)) if token.isdigit() else token
        for token in re.findall(_NUMBER_PATTERN, cleaned)
    }
    return times | numbers


def _case_data_tokens(data: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, (str, int, float)):
            tokens.update(_numeric_tokens(str(value)))

    collect(data)
    return tokens


def unsupported_numbers(
    step: TestStep,
    rules: dict[str, RequirementRule],
    evidence: dict[str, EvidenceSpan],
    allowed_numbers: set[str] | None = None,
) -> list[str]:
    """预期里既不在引文、动作、数据或程序推导边界中的数字。"""
    quote = "\n".join(evidence[e].quote for e in step.evidence_ids if e in evidence)
    known = _numeric_tokens(quote) | _numeric_tokens(step.action)
    known |= allowed_numbers or set()
    for rule_id in step.rule_ids:
        rule = rules.get(rule_id)
        if rule is not None:
            known.update(_numeric_tokens(" ".join(boundary_values(rule))))
    known.update(_CONTRAST_PATTERN.findall(step.expected))
    return [
        number
        for number in sorted(_numeric_tokens(step.expected))
        if number not in known
    ]


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
        signature = case_signature(case)
        duplicated = signature in signatures
        if duplicated:
            issues.append(issue("duplicate", "步骤及输入完全重复，保留并标记", [case.case_id]))
            flagged = True
        signatures.add(signature)
        for number, step in enumerate(case.steps, 1):
            expected_evidence = {e for r in step.rule_ids for e in rm[r].evidence_ids}
            quote = "\n".join(em[e].quote for e in step.evidence_ids)
            unsupported = unsupported_numbers(step, rm, em, _case_data_tokens(case.data))
            step_flagged = not set(step.evidence_ids) <= expected_evidence or bool(unsupported)
            if step_flagged:
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
            status: Literal["validated", "needs_review"] = (
                "needs_review" if step_flagged or duplicated else "validated"
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
                                validation_status=status,
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
