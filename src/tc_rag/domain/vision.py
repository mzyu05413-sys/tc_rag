"""视觉解释契约：约束来源和关联，不把结构合法等同于识别正确。"""

from typing import Any, Literal

from pydantic import Field, model_validator

from tc_rag.domain.models import DomainModel


class VisualFact(DomainModel):
    fact_id: str = Field(min_length=1)
    kind: Literal["text", "field", "constraint", "action", "state", "role", "feedback"]
    text: str = Field(min_length=1)
    view_ids: list[str] = Field(min_length=1)


class VisualRelation(DomainModel):
    kind: Literal["flow", "condition", "interaction", "state", "permission", "dependency"]
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)


class VisualTable(DomainModel):
    title: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(min_length=1)
    view_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def rectangular(self) -> "VisualTable":
        if self.headers and any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("表格列数不一致；不确定的合并关系应记录为疑点")
        return self


class VisualContextLink(DomainModel):
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)
    context_ids: list[str] = Field(min_length=1)


class VisualUncertainty(DomainModel):
    text: str = Field(min_length=1)
    view_ids: list[str] = Field(min_length=1)


class VisionDescription(DomainModel):
    image_type: Literal["ui", "flowchart", "table", "mixed", "other"]
    facts: list[VisualFact] = Field(default_factory=list)
    relations: list[VisualRelation] = Field(default_factory=list)
    tables: list[VisualTable] = Field(default_factory=list)
    context_links: list[VisualContextLink] = Field(default_factory=list)
    uncertainties: list[VisualUncertainty] = Field(default_factory=list)

    def check_references(self, views: set[str], contexts: set[str]) -> None:
        """不接受虚构的局部图、正文片段和事实 ID。"""
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("视觉事实 ID 重复")
        known = set(ids)
        if not (self.facts or self.tables or self.uncertainties):
            raise ValueError("视觉解释为空")
        sourced: list[VisualFact | VisualTable | VisualUncertainty] = [
            *self.facts, *self.tables, *self.uncertainties
        ]
        for item in sourced:
            if not set(item.view_ids) <= views:
                raise ValueError("视觉解释引用了未提供的视图")
        linked: list[VisualRelation | VisualContextLink] = [*self.relations, *self.context_links]
        for relation in linked:
            if not set(relation.fact_ids) <= known:
                raise ValueError("视觉关系引用了不存在的事实")
        for link in self.context_links:
            if not set(link.context_ids) <= contexts:
                raise ValueError("图文关联引用了未提供的正文")


class VisionAudit(DomainModel):
    """只扩展视觉 JSON 载荷，历史三字段缓存继续兼容。"""

    description: VisionDescription
    draft: str
    draft_valid: bool
    model_verified: bool
    human_verified: Literal[False] = False
    verify_error: str | None = None
    review_model: str | None = None
    context: dict[str, Any]
    views: list[dict[str, Any]] = Field(min_length=1)
