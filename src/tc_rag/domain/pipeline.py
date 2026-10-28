"""P1 阶段输出和可恢复快照；业务事实仍由 SQLite 持有。"""

from typing import Any

from pydantic import Field, model_validator

from tc_rag.domain.models import (
    BusinessEntity,
    DomainModel,
    EvidenceSpan,
    Feature,
    Issue,
    RequirementRule,
    TestCase,
    TestPoint,
)


class Extraction(DomainModel):
    rule_mapping: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    features: list[Feature] = Field(default_factory=list)
    rules: list[RequirementRule] = Field(default_factory=list)
    entities: list[BusinessEntity] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_object_ids(self) -> "Extraction":
        """ID 只需在同类对象内唯一；跨类型复用是允许的，引用字段本身区分类型。"""
        checks = (
            ("evidence", [value.evidence_id for value in self.evidence]),
            ("feature", [value.feature_id for value in self.features]),
            ("rule", [value.rule_id for value in self.rules]),
            ("entity", [value.entity_id for value in self.entities]),
            ("issue", [value.issue_id for value in self.issues]),
        )
        for name, ids in checks:
            if len(ids) != len(set(ids)):
                # 固定说明不携带重复 ID 或正文，避免异常进入终端时泄露输入内容。
                raise ValueError(f"Extraction {name} IDs must be unique")
        return self


class CaseBatch(DomainModel):
    cases: list[TestCase] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class PointBatch(DomainModel):
    points: list[TestPoint] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class StageSnapshot(DomainModel):
    snapshot_id: str
    run_id: str
    stage: str
    payload: dict[str, Any]
