"""知识库完整文档批次的持久化材料。"""

from pydantic import Field

from tc_rag.domain.kb.models import KnowledgeSnapshot
from tc_rag.domain.models import Asset, Chunk, DocumentBlock, DomainModel, Issue


class IndexedSnapshot(DomainModel):
    snapshot: KnowledgeSnapshot
    blocks: list[DocumentBlock]
    chunks: list[Chunk]
    issues: list[Issue]
    vector_asset: Asset
    index_version: str = Field(min_length=1)
    metric: str
    original_block_ids: dict[str, str] = Field(default_factory=dict)


class IngestionJob(DomainModel):
    job_id: str
    kb_id: str
    status: str
    error_type: str | None = None
