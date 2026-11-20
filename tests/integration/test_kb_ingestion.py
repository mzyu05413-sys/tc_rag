"""真实解析/索引与 Fake embedding 验证知识库完整批次发布。"""

from dataclasses import replace

import pytest
from docx import Document

from tc_rag.application.kb.ingest import IngestionService
from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.parsing.docx import DocxParser
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.kb.sqlite import KnowledgeSqliteRepository


def setup(tmp_path):
    repository = KnowledgeSqliteRepository(tmp_path / "kb.sqlite")
    store = LocalArtifactStore(tmp_path / "assets")
    embedder = FakeEmbedder()
    service = IngestionService(
        repository, store, lambda suffix: DocxParser(store), embedder,
        EmbeddingSpace("fixture", "m", 3), lambda: NumpyIndex("cosine"),
    )
    paths = {}
    for name in ("approval", "order"):
        document = Document()
        document.add_paragraph("会签需要全员同意。")
        path = tmp_path / f"{name}.docx"
        document.save(path)
        paths[name] = path
    return service, repository, embedder, paths


@pytest.mark.asyncio
async def test_complete_batch_idempotence_update_and_filter(tmp_path):
    service, repository, embedder, paths = setup(tmp_path)
    first = await service.ingest("crm", paths)
    assert len(first.snapshot.documents) == 2
    assert len({b.block_id for b in first.blocks}) == len(first.blocks)
    calls = embedder.calls
    repeated = await service.ingest("crm", paths)
    assert repeated == first
    assert embedder.calls == calls
    updated = await service.ingest("crm", {"approval": paths["approval"]})
    assert [d.document_id for d in updated.snapshot.documents] == ["approval"]
    assert repository.active("crm") == updated
    assert first.snapshot.snapshot_id != updated.snapshot.snapshot_id


@pytest.mark.asyncio
async def test_failed_embedding_keeps_previous_snapshot(tmp_path):
    service, repository, embedder, paths = setup(tmp_path)
    first = await service.ingest("crm", paths)
    service.embedder = FakeEmbedder([RuntimeError("offline failure")])
    # 空间变化强制重建，模型失败不得发布半成品。
    service.space = replace(service.space, model="new")
    with pytest.raises(RuntimeError):
        await service.ingest("crm", paths)
    assert repository.active("crm") == first


@pytest.mark.asyncio
async def test_invalid_vectors_and_partial_source_failure_keep_head(tmp_path):
    service, repository, _, paths = setup(tmp_path)
    first = await service.ingest("crm", paths)
    service.embedder = FakeEmbedder([[[float("nan"), 0, 1]]])
    with pytest.raises(ValueError):
        await service.ingest("crm", {"approval": paths["approval"]})
    assert repository.active("crm") == first
    with pytest.raises(FileNotFoundError):
        await service.ingest("crm", {**paths, "missing": tmp_path / "absent.docx"})
    assert repository.active("crm") == first


@pytest.mark.asyncio
async def test_concurrent_publication_rejects_stale_head(tmp_path):
    service, repository, _, paths = setup(tmp_path)
    first = await service.ingest("crm", paths)
    with pytest.raises(ValueError, match="其他任务"):
        repository.publish(first, None)
    assert repository.active("crm") == first


@pytest.mark.asyncio
async def test_preflight_failure_precedes_paid_vision(tmp_path):
    service, repository, _, paths = setup(tmp_path)
    service.embedder = FakeEmbedder([RuntimeError("preflight failure")])

    class ForbiddenVision:
        progress = None

        async def process(self, blocks):
            raise AssertionError("预检失败后不能进入图片处理")

    service.vision = ForbiddenVision()
    with pytest.raises(RuntimeError, match="preflight failure"):
        await service.ingest("crm", paths, preflight=True)
    assert repository.active("crm") is None
