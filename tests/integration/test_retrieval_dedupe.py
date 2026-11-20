"""来源去重接入（A10）：retrieve 在 dedupe_sources 开启时折叠同源补充证据。"""

import pytest

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.domain.models import EvidenceSpan, Locator
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.context import RagContext
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_retrieve_dedupes_same_source_when_enabled(tmp_path):
    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    # 首次批：两个唯一 quote 的向量；第二次批：查询向量。
    embedder = FakeEmbedder([[[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0]]])
    context = RagContext(
        repo, store, embedder, EmbeddingSpace("fixture", "m", 2),
        "cosine", 5, "run", dedupe_sources=True,
    )
    await context.prepare("doc", [
        EvidenceSpan(evidence_id="a", block_id="b", quote="最多300个", locator=Locator()),
        EvidenceSpan(evidence_id="b", block_id="b", quote="最多300个", locator=Locator()),
        EvidenceSpan(evidence_id="c", block_id="b", quote="数量为整数", locator=Locator()),
    ])
    bundle = await context.retrieve("最多", set())
    assert [e["evidence_id"] for e in bundle["supplemental_evidence"]] == ["a", "c"]
    assert bundle["coverage"] == {"a": ["b"]}


@pytest.mark.asyncio
async def test_retrieve_keeps_all_when_dedupe_disabled(tmp_path):
    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    embedder = FakeEmbedder([[[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0]]])
    context = RagContext(
        repo, store, embedder, EmbeddingSpace("fixture", "m", 2),
        "cosine", 5, "run",
    )
    await context.prepare("doc", [
        EvidenceSpan(evidence_id="a", block_id="b", quote="最多300个", locator=Locator()),
        EvidenceSpan(evidence_id="b", block_id="b", quote="最多300个", locator=Locator()),
        EvidenceSpan(evidence_id="c", block_id="b", quote="数量为整数", locator=Locator()),
    ])
    bundle = await context.retrieve("最多", set())
    assert [e["evidence_id"] for e in bundle["supplemental_evidence"]] == ["a", "b", "c"]
    assert bundle["coverage"] == {}
