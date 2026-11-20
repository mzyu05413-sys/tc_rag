"""功能目录候选（A1 报告先行）：报告可能属于同一业务功能的功能片段，只报告不合并。

v1 只用确定性信号：名称完全相同、或共享同一证据。两者都直接对应“同一业务功能
跨块拆成多个 Feature”的问题。按章节路径 / 业务对象 / 操作范围聚簇需要额外解析
块上下文与语义抽取，不在本报告先行阶段推测；本函数不自动合并、不回写原文。
"""

from collections.abc import Sequence

from tc_rag.application.services.design._common import issue
from tc_rag.domain.models import Feature, Issue


def catalog_feature_candidates(features: Sequence[Feature]) -> list[Issue]:
    """报告同名或证据重叠的功能候选，供人工核对是否同一业务功能。"""
    issues: list[Issue] = []

    by_name: dict[str, list[str]] = {}
    for feature in features:
        name = feature.name.strip()
        if name:
            by_name.setdefault(name, []).append(feature.feature_id)
    for feature_ids in by_name.values():
        if len(feature_ids) > 1:
            issues.append(
                issue(
                    "gap",
                    "多个功能名称完全相同，可能是同一业务功能被跨块拆散，需人工核对是否合并",
                    sorted(feature_ids),
                    severity="info",
                )
            )

    by_evidence: dict[str, set[str]] = {}
    for feature in features:
        for evidence_id in feature.evidence_ids:
            by_evidence.setdefault(evidence_id, set()).add(feature.feature_id)
    reported: set[tuple[str, ...]] = set()
    for shared in by_evidence.values():
        unique = tuple(sorted(shared))
        if len(unique) > 1 and unique not in reported:
            reported.add(unique)
            issues.append(
                issue(
                    "gap",
                    "不同功能共享同一证据，可能是同一业务功能的片段，需人工核对功能归属",
                    list(unique),
                    severity="info",
                )
            )
    return issues
