"""真实 SQLite 缓存复用、空间隔离与损坏拒绝。"""

from dataclasses import replace

import pytest

from tc_rag.application.kb.embedding_cache import CachedEmbedder
from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.storage.kb.sqlite import KnowledgeSqliteRepository


@pytest.mark.asyncio
async def test_cache_deduplicates_and_separates_space(tmp_path):
    delegate = FakeEmbedder()
    cache = CachedEmbedder(delegate, KnowledgeSqliteRepository(tmp_path / "db"))
    space = EmbeddingSpace("fixture", "m", 3)
    first = await cache.embed_batch(["a", "a", "b"], space)
    assert first.vectors[0] == first.vectors[1]
    assert delegate.calls == 1
    assert (await cache.embed_batch(["a", "b"], space)).vectors == [
        first.vectors[0], first.vectors[2],
    ]
    assert delegate.calls == 1
    await cache.embed_batch(["a"], replace(space, model="m2"))
    assert delegate.calls == 2
