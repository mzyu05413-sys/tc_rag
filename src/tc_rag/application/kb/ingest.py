"""串行解析完整成员集，索引校验后发布快照。"""

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from tc_rag.application.kb.input_limits import bound_chunks
from tc_rag.application.ports.contracts import (
    ArtifactStore,
    DocumentParser,
    Embedder,
    EmbeddingSpace,
    VectorIndex,
)
from tc_rag.application.ports.kb import KnowledgeRepository
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.services.chunking import TokenCounter, character_estimate, structural_chunks
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.kb.ingestion import IndexedSnapshot, IngestionJob
from tc_rag.domain.kb.models import DocumentVersion, KnowledgeSnapshot
from tc_rag.domain.models import Asset, DocumentBlock, Issue


class BlockProcessor(Protocol):
    progress: ProgressReporter | None

    async def process(
        self, blocks: list[DocumentBlock],
    ) -> tuple[list[DocumentBlock], list[Issue]]: ...


class PublishableIndex(VectorIndex, Protocol):
    version: str

    def publish(self, store: ArtifactStore) -> Asset: ...

    def load(
        self, store: ArtifactStore, asset: Asset, ids: list[str], space: EmbeddingSpace,
        expected_version: str,
    ) -> None: ...


class IngestionService:
    def __init__(
        self, repository: KnowledgeRepository, store: ArtifactStore,
        parser: Callable[[str], DocumentParser], embedder: Embedder,
        space: EmbeddingSpace, index_factory: Callable[[], PublishableIndex],
        counter: TokenCounter = character_estimate, counter_id: str = "character_approx",
    ) -> None:
        self.repository, self.store, self.parser = repository, store, parser
        self.embedder, self.space, self.index_factory = embedder, space, index_factory
        self.counter, self.counter_id = counter, counter_id
        self.chunk_options: dict[str, Any] = {}
        self.processing_identity: dict[str, Any] = {}
        self.vision: BlockProcessor | None = None
        self.runtime_identity: dict[str, Any] = {}
        self.progress: ProgressReporter | None = None
        self.max_embedding_bytes: int | None = None
        self.preflight_embedder: Embedder | None = None

    async def ingest(
        self, kb_id: str, documents: dict[str, Path], *,
        product_versions: dict[str, str | None] | None = None,
        target: int = 900, overlap: int = 100,
        preflight: bool = False,
    ) -> IndexedSnapshot:
        """documents 是完整目标成员集；同 ID 替换，省略成员从新快照移除。"""
        if not kb_id.strip() or not documents:
            raise ValueError("知识库和文档集合不能为空")
        if product_versions is not None and not set(product_versions).issubset(documents):
            raise ValueError("产品版本清单包含本批次以外的文档")
        original = self.repository.active(kb_id)
        job = IngestionJob(job_id=new_id("kbjob"), kb_id=kb_id, status="processing")
        self.repository.save_job(job)
        try:
            if preflight:
                report(self.progress, ProgressEvent("stage", "kb_preflight"))
                preflight_embedder = self.preflight_embedder or self.embedder
                probe = await preflight_embedder.embed_batch(["知识库向量预检。"], self.space)
                if probe.space != self.space:
                    raise ValueError("向量预检空间不一致")
                self.index_factory().build(["probe"], probe.vectors, self.space)
            report(self.progress, ProgressEvent("stage", "kb_ingest"))
            versions, blocks, chunks = [], [], []
            issues: list[Issue] = []
            original_block_ids: dict[str, str] = {}
            for position, (document_id, path) in enumerate(sorted(documents.items()), 1):
                report(self.progress, ProgressEvent(
                    "batch_started", "kb_ingest", current=position, total=len(documents),
                ))
                if not document_id.strip():
                    raise ValueError("逻辑文档 ID 不能为空")
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                revision = fingerprint([kb_id, document_id, digest])
                asset = self.store.put(f"originals/{revision}{path.suffix.lower()}", content)
                parsed = self.parser(path.suffix.lower()).parse(asset)
                if self.vision is not None:
                    has_images = any(block.kind == "image" for block in parsed.blocks)
                    if has_images:
                        report(self.progress, ProgressEvent("stage", "vision"))
                    previous = self.vision.progress
                    self.vision.progress = self.progress
                    try:
                        parsed.blocks, vision_issues = await self.vision.process(parsed.blocks)
                    finally:
                        self.vision.progress = previous
                    if has_images:
                        report(self.progress, ProgressEvent("stage", "kb_ingest"))
                    parsed.issues.extend(vision_issues)
                # 同内容的不同逻辑文档也保留独立来源命名空间。
                scoped = [block.model_copy(update={
                    "block_id": f"{revision}:{block.order}", "document_id": revision,
                }) for block in parsed.blocks]
                reference_map = {
                    old.block_id: new.block_id for old, new in zip(parsed.blocks, scoped)
                }
                reference_map.update({b.document_id: revision for b in parsed.blocks})
                original_block_ids.update({
                    new.block_id: old.block_id for old, new in zip(parsed.blocks, scoped)
                })
                result = structural_chunks(
                    scoped, target, overlap, self.counter, **self.chunk_options,
                )
                result.chunks = bound_chunks(
                    result.chunks, scoped, self.max_embedding_bytes, self.counter,
                )
                if not result.chunks:
                    raise ValueError("文档没有可索引文本，需核对图片或扫描内容")
                for chunk in result.chunks:
                    old_chunk_id = chunk.chunk_id
                    chunk.chunk_id = fingerprint([
                        revision, chunk.fingerprint,
                        [source.model_dump() for source in chunk.sources],
                    ])
                    reference_map[old_chunk_id] = chunk.chunk_id
                versions.append(DocumentVersion(
                    document_id=document_id, version_id=revision, content_sha256=digest,
                    product_version=(product_versions or {}).get(document_id),
                ))
                blocks.extend(scoped)
                chunks.extend(result.chunks)
                issues.extend(issue.model_copy(update={
                    "issue_id": fingerprint([revision, issue.kind, issue.description, number]),
                    "object_ids": [reference_map.get(key, key) for key in issue.object_ids],
                }) for number, issue in enumerate(parsed.issues + result.issues))
                report(self.progress, ProgressEvent(
                    "batch_completed", "kb_ingest", completed=position, total=len(documents),
                ))
            report(self.progress, ProgressEvent("stage", "index"))
            index = self.index_factory()
            # 版本在内容和配置一致时稳定，Chunk 的随机标识不参与身份计算。
            identity: list[Any] = [
                kb_id, [v.model_dump() for v in versions], asdict(self.space), target, overlap,
                [c.model_dump() for c in chunks], type(index).__name__,
                getattr(getattr(index, "metric", None), "name", None),
                self.counter_id,
                self.chunk_options, self.processing_identity,
                self.max_embedding_bytes,
            ]
            snapshot_id = fingerprint(identity)
            if original and original.snapshot.snapshot_id == snapshot_id:
                self.store.read(original.vector_asset)
                job.status = "ready"
                self.repository.save_job(job)
                report(self.progress, ProgressEvent("stage", "completed"))
                return original
            batch = await self.embedder.embed_batch([c.text for c in chunks], self.space)
            if batch.space != self.space:
                raise ValueError("向量返回空间与请求不一致")
            index.build([c.chunk_id for c in chunks], batch.vectors, self.space)
            vector_asset = index.publish(self.store)
            self.store.read(vector_asset)
            value = IndexedSnapshot(
                snapshot=KnowledgeSnapshot(
                    kb_id=kb_id, snapshot_id=snapshot_id,
                    embedding_space_id=fingerprint(asdict(self.space)), documents=versions,
                ), blocks=blocks, chunks=chunks, issues=issues, vector_asset=vector_asset,
                index_version=index.version,
                metric=str(getattr(getattr(index, "metric", None), "name", "unknown")),
                original_block_ids=original_block_ids,
            )
            report(self.progress, ProgressEvent("stage", "kb_publish"))
            self.repository.publish(
                value, original.snapshot.snapshot_id if original else None,
            )
            job.status = "ready"
            self.repository.save_job(job)
            report(self.progress, ProgressEvent("stage", "completed"))
            return value
        except Exception as error:
            job.status, job.error_type = "failed", type(error).__name__
            self.repository.save_job(job)
            report(self.progress, ProgressEvent("stage", "failed"))
            raise
