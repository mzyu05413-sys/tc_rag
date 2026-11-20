"""仅检索返回原文来源，范围错误不扩大为全库。"""

import pytest
from test_kb_ingestion import setup

from tc_rag.application.kb.search import SearchService
from tc_rag.infrastructure.retrieval.keyword import KeywordIndex, rrf


@pytest.mark.asyncio
async def test_search_filter_sources_and_snapshot(tmp_path):
    components, _, _, paths = setup(tmp_path)
    snapshot = await components.ingest("crm", paths)
    service = SearchService(components)
    result = await service.search("crm", "会签", document_ids={"order"})
    revision = next(d.version_id for d in snapshot.snapshot.documents if d.document_id == "order")
    assert result["snapshot_id"] == snapshot.snapshot.snapshot_id
    assert result["candidates"]
    assert all(s["document_version"] == revision
               for c in result["candidates"] for s in c["sources"])
    calls = components.embedder.calls
    assert (await service.search("crm", "会签", document_ids=set()))["candidates"] == []
    assert components.embedder.calls == calls
    with pytest.raises(ValueError, match="快照外"):
        await service.search("crm", "会签", document_ids={"absent"})
    with pytest.raises(ValueError, match="尚未发布"):
        await service.search("crm", "会签", snapshot_id="stale")


@pytest.mark.asyncio
async def test_corrupted_index_does_not_call_embedding(tmp_path):
    components, _, _, paths = setup(tmp_path)
    snapshot = await components.ingest("crm", paths)
    (components.store.root / snapshot.vector_asset.relative_path).write_bytes(b"broken")
    calls = components.embedder.calls
    with pytest.raises(ValueError):
        await SearchService(components).search("crm", "会签")
    assert components.embedder.calls == calls


@pytest.mark.asyncio
async def test_historical_snapshot_and_rerank_degradation(tmp_path):
    components, _, _, paths = setup(tmp_path)
    original = await components.ingest("crm", paths)
    await components.ingest("crm", {"order": paths["order"]})

    class BrokenReranker:
        async def rank(self, query, candidates):
            raise RuntimeError("fixture")

    service = SearchService(
        components, keyword=KeywordIndex(tokenizer=list), fusion=rrf,
        reranker=BrokenReranker(), mode="hybrid", rerank_enabled=True,
    )
    result = await service.search(
        "crm", "会签", snapshot_id=original.snapshot.snapshot_id, document_ids={"approval"},
    )
    assert result["candidates"]
    assert result["snapshot_id"] == original.snapshot.snapshot_id
    assert result["rerank_degradation"] == "RuntimeError"
    assert result["candidates"][0]["fusion_score"] is not None
