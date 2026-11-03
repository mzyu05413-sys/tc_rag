"""阶段快照的结构校验与错误汇总。"""

from typing import Any

from pydantic import ValidationError

from tc_rag.application.services.quality._common import SnapshotMap
from tc_rag.application.services.quality._common import case_point as _case_point
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction
from tc_rag.domain.rag import ContextPayload
from tc_rag.domain.review import PointTree


def _validate_stage(stage: str, payload: dict[str, Any]) -> None:
    if stage == "chunk":
        m.Document.model_validate(payload["document"])
        for block in payload.get("blocks", []):
            m.DocumentBlock.model_validate(block)
        for chunk in payload.get("chunks", []):
            m.Chunk.model_validate(chunk)
        for item in payload.get("issues", []):
            m.Issue.model_validate(item)
    elif stage == "extract":
        Extraction.model_validate(payload)
    elif stage == "plan_points":
        for point in payload.get("points", []):
            m.TestPoint.model_validate(point)
    elif stage == "point_tree":
        PointTree.model_validate(payload)
    elif stage == "cases":
        for case in payload.get("cases", []):
            m.TestCase.model_validate(case)
        for point in payload.get("points", []):
            _case_point(point)
        for evidence in payload.get("evidence", []):
            m.EvidenceSpan.model_validate(evidence)
        for feature in payload.get("features", []):
            m.Feature.model_validate(feature)
        for rule in payload.get("rules", []):
            m.RequirementRule.model_validate(rule)
        for link in payload.get("coverage", []):
            m.CoverageLink.model_validate(link)
        for item in payload.get("issues", []):
            m.Issue.model_validate(item)
    elif stage == "retrieval":
        for bundle in payload.get("bundles", []):
            ContextPayload.model_validate(bundle)
    elif stage == "invocations":
        for item in payload.get("items", []):
            m.ModelInvocation.model_validate(item)


def _schema_errors(snapshots: SnapshotMap) -> dict[str, str]:
    errors: dict[str, str] = {}
    for stage in (
        "chunk",
        "extract",
        "plan_points",
        "point_tree",
        "cases",
        "retrieval",
        "invocations",
    ):
        payload = snapshots.get(stage)
        if payload is None:
            continue
        try:
            _validate_stage(stage, payload)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            errors[stage] = type(exc).__name__
    return errors


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
validate_stage = _validate_stage
schema_errors = _schema_errors
