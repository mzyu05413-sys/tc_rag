"""RAG 持久化记录；供应商 SDK、文件和索引不进入领域层。"""

from typing import Any, Literal

from pydantic import Field, model_validator

from tc_rag.domain.models import Asset, DomainModel, EvidenceSpan


class SpacePayload(DomainModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    normalize: bool
    template_version: str = Field(min_length=1)


class EmbeddingPayload(DomainModel):
    space_id: str = Field(min_length=1)
    text_fingerprint: str = Field(min_length=1)
    vector: list[float] = Field(min_length=1)
    vector_fingerprint: str = Field(min_length=1)


class RequirementSetPayload(DomainModel):
    document_fingerprints: list[str] = Field(min_length=1)


class ManifestPayload(DomainModel):
    scope: str
    space_id: str
    metric: str
    version: str
    asset: Asset
    ids: list[str]
    vector_ids: list[int]
    backend: str
    run_id: str

    @model_validator(mode="after")
    def check_mapping(self) -> "ManifestPayload":
        if (
            len(self.ids) != len(self.vector_ids)
            or len(set(self.ids)) != len(self.ids)
            or len(set(self.vector_ids)) != len(self.vector_ids)
            or any(not 0 < i < 2**63 for i in self.vector_ids)
        ):
            raise ValueError("manifest 业务 ID 与 int64 映射不一致")
        return self


class ContextPayload(DomainModel):
    query_id: str
    scope: str
    space_id: str
    index_version: str
    direct_ids: list[str]  # 当前规则绑定的原文证据 ID，不代表另一条生成路线。
    hits: list[dict[str, Any]]
    vector_hits: list[dict[str, Any]]
    keyword_ids: list[str]
    fusion: list[dict[str, Any]]
    rerank: dict[str, Any]
    supplemental_evidence: list[EvidenceSpan]
    query_embedding_cache_hit: bool
    timings_ms: dict[str, float]
    selection: str
    adopted_ids: list[str] = Field(default_factory=list)
    omitted_ids: list[str] = Field(default_factory=list)
    # 仅供旧数据库上下文读取；已移除对应运行支路，新序列化结果不携带历史审计字段。
    code_adopted_ids: list[str] = Field(default_factory=list, exclude=True, repr=False)
    code_omitted_ids: list[str] = Field(default_factory=list, exclude=True, repr=False)
    generation: dict[str, Any] = Field(default_factory=dict)


class VisionPayload(DomainModel):
    text: str = Field(min_length=1)
    model: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RagRecord(DomainModel):
    record_id: str = Field(min_length=1)
    kind: Literal["space", "embedding", "manifest", "requirement_set", "context", "query", "vision"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def check_payload(self) -> "RagRecord":
        # JSON 编码不等于无结构：每一类事实在读写时执行独立领域契约校验。
        schemas: dict[str, type[DomainModel]] = {
            "space": SpacePayload,
            "embedding": EmbeddingPayload,
            "requirement_set": RequirementSetPayload,
            "manifest": ManifestPayload,
            "context": ContextPayload,
            "vision": VisionPayload,
        }
        if self.kind in schemas:
            schemas[self.kind].model_validate(self.payload)
        return self
