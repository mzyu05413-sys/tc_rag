"""跨时期 PRD 以显式版本集合参与检索，业务版本未知时保留空值。"""

from pydantic import Field, model_validator

from tc_rag.domain.models import DomainModel


class DocumentVersion(DomainModel):
    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_version: str | None = Field(default=None, min_length=1)


class KnowledgeSnapshot(DomainModel):
    kb_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    index_format_version: str = "1.0"
    embedding_space_id: str = Field(min_length=1)
    documents: list[DocumentVersion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_documents(self) -> "KnowledgeSnapshot":
        document_ids = [item.document_id for item in self.documents]
        version_ids = [item.version_id for item in self.documents]
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("同一快照每个逻辑文档只能绑定一个版本")
        if len(set(version_ids)) != len(version_ids):
            raise ValueError("文档版本标识必须唯一")
        return self
