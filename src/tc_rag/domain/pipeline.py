"""P1 阶段输出和可恢复快照；业务事实仍由 SQLite 持有。"""

from collections import Counter
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


def case_batch_schema(
    point_ids: list[str], max_cases_per_point: int | None = None
) -> type[CaseBatch]:
    """把批次引用和数量上限前置校验，让错误进入有界修复且不写入有效缓存。"""
    allowed = sorted(set(point_ids))
    if not allowed:
        raise ValueError("用例批次至少需要一个测试点")
    if max_cases_per_point is not None and max_cases_per_point < 1:
        raise ValueError("单点用例上限必须为正整数")
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
    validators: dict[str, Any] = {}
    if max_cases_per_point is not None:
        def check_point_limit(batch: CaseBatch) -> CaseBatch:
            counts: Counter[str] = Counter()
            for case in batch.cases:
                # 一个用例即便重复写同一引用，也只按一个关联计数，与最终检查一致。
                counts.update(set(case.point_ids))
            if any(count > max_cases_per_point for count in counts.values()):
                # 固定错误说明不携带实际 ID、用例内容或输入值。
                raise ValueError("CaseBatch per-point case count exceeds configured limit")
            return batch

        validators["check_point_limit"] = model_validator(mode="after")(check_point_limit)
    case_list = (
        conlist(case_type, max_length=len(allowed) * max_cases_per_point)
        if max_cases_per_point is not None else conlist(case_type)
    )
    # 沿用其余严格约束；不增加数量、不截断结果、不重写引用。
    return create_model(
        "CaseBatch",
        __base__=CaseBatch,
        __validators__=validators,
        cases=(case_list, Field(default_factory=list)),
    )


class PointBatch(DomainModel):
    points: list[TestPoint] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class StageSnapshot(DomainModel):
    snapshot_id: str
    run_id: str
    stage: str
    payload: dict[str, Any]
