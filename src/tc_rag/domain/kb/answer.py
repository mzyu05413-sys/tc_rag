"""回答由可核对的原子声明和引文构成。"""

from typing import Literal

from pydantic import Field, model_validator

from tc_rag.domain.models import DomainModel


class Citation(DomainModel):
    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class AnswerClaim(DomainModel):
    text: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)


class KnowledgeAnswer(DomainModel):
    status: Literal["answered", "abstained"]
    claims: list[AnswerClaim] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> "KnowledgeAnswer":
        if self.status == "answered" and not self.claims:
            raise ValueError("回答必须有带引文的声明")
        if self.status == "abstained" and (self.claims or not self.reason):
            raise ValueError("拒答须说明原因且不附事实声明")
        return self
