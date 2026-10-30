"""P1 阶段输出和可恢复快照；业务事实仍由 SQLite 持有。"""

from collections.abc import Callable
from enum import Enum
from typing import Any, cast

from pydantic import Field, conlist, create_model, model_validator

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


def case_batch_schema(point_ids: list[str]) -> type[CaseBatch]:
    """把当前批次的测试点绑定为枚举，让错误引用进入已有 Schema 修复流程。"""
    allowed = sorted(set(point_ids))
    if not allowed:
        raise ValueError("用例批次至少需要一个测试点")
    # 枚举值来自输入事实；不按标题猜测或重写模型引用。排序保证同一范围的契约指纹稳定。
    # 成员来自运行时输入，只依赖枚举类型接口，不声明可供静态访问的固定成员。
    enum_factory = cast(Callable[..., type[Enum]], Enum)
    point_type = enum_factory(
        "BatchPointId", {f"point_{index}": point_id for index, point_id in enumerate(allowed)},
        type=str,
    )
    case_type = create_model(
        "ScopedTestCase",
        __base__=TestCase,
        point_ids=(conlist(point_type, min_length=1), ...),
    )
    # 沿用 CaseBatch 的字段、默认值和严格校验；只缩小 point_ids 的合法范围。
    return create_model(
        "CaseBatch",
        __base__=CaseBatch,
        cases=(conlist(case_type), Field(default_factory=list)),
    )


class PointBatch(DomainModel):
    points: list[TestPoint] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class StageSnapshot(DomainModel):
    snapshot_id: str
    run_id: str
    stage: str
    payload: dict[str, Any]
