"""虚构向量与证据验证范围隔离、缓存空间隔离和索引损坏拒绝。"""

import httpx
import pytest
from openai import AsyncOpenAI

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.domain.models import EvidenceSpan, Locator
from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.context import RagContext
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_rag_two_stage_pipeline_uses_retrieval(tmp_path, monkeypatch):
    from test_pipeline import FixtureLLM, setup

    from tc_rag.bootstrap import build_pipeline

    original, document, _, _ = setup(tmp_path)
    cfg = original.config.model_copy(deep=True)
    cfg.retrieval.embedding_dimension = 3
    cfg.models.embedding.model = "fixture"
    model = FixtureLLM()
    embedder = FakeEmbedder()
    pipeline = build_pipeline(cfg, "rag_run", model, embedder)
    await pipeline.points(document, "边界测试点", tmp_path / "out", run_id="rag_run")
    bundles = pipeline.read("rag_run", "retrieval")["bundles"]
    assert bundles and bundles[0]["index_version"]
    assert bundles[0]["direct_ids"]
    assert pipeline.manifest("rag_run")["mode"] == "rag"
    assert embedder.calls > 0
    # 同一流程的第二次执行也必须实际检索，而不只在首轮建一个闲置索引。
    from tc_rag.application.requests import CasesRequest

    request = CasesRequest(
        run_id="rag_run", instruction="generate cases", output_dir=tmp_path / "out"
    )
    await pipeline.cases(request)
    assert pipeline.read("rag_run", "cases")["mode"] == "rag"
    assert len(pipeline.read("rag_run", "retrieval")["bundles"]) >= 2
    calls = model.calls

    async def unavailable(query, direct_ids):
        raise RuntimeError("fixture-retrieval-unavailable")

    monkeypatch.setattr(pipeline.retriever, "retrieve", unavailable)
    request = CasesRequest.model_validate(pipeline.read("rag_run", "generation_request"))
    # 即使已有生成缓存，检索失败也不能跳过 RAG 直接复用用例输出。
    with pytest.raises(RuntimeError, match="fixture-retrieval-unavailable"):
        await pipeline.cases(request)
    assert model.calls == calls
    assert pipeline.read("rag_run", "failure")["stage"] == "generate"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["index", "retrieve"])
async def test_index_or_retrieval_failure_never_falls_back_to_generation(
    tmp_path,
    monkeypatch,
    failed_stage,
):
    from test_pipeline import setup

    pipeline, document, _, model = setup(tmp_path)

    async def unavailable(*args):
        raise RuntimeError("fixture-rag-unavailable")

    monkeypatch.setattr(
        pipeline.retriever,
        "prepare" if failed_stage == "index" else "retrieve",
        unavailable,
    )
    with pytest.raises(RuntimeError, match="fixture-rag-unavailable"):
        await pipeline.points(document, "生成测试点", tmp_path / "out", run_id="run_failed_rag")
    assert model.calls == 1  # 只完成提炼，不能继续规划或导出。
    assert pipeline.read("run_failed_rag", "failure")["stage"] == (
        "index" if failed_stage == "index" else "plan_points"
    )
    assert not list((tmp_path / "out" / "run_failed_rag").glob("testpoints*.xmind"))


def test_filter_before_topk_and_metric_direction(tmp_path):
    space = EmbeddingSpace("fixture", "m", 2)
    for metric in ("cosine", "euclidean", "dot"):
        index = NumpyIndex(metric)
        index.build(["foreign", "allowed"], [[1, 0], [0, 1]], space)
        hits = index.search([1, 0], 1, {"allowed"})
        assert [h.object_id for h in hits] == ["allowed"]
        assert index.search([1, 0], 1, set()) == []
        store = LocalArtifactStore(tmp_path / metric)
        asset = index.publish(store)
        loaded = NumpyIndex(metric)
        loaded.load(store, asset, index.ids, space, index.version)
        assert loaded.search([1, 0], 2) == index.search([1, 0], 2)
        with pytest.raises(ValueError, match="manifest"):
            loaded.load(store, asset, list(reversed(index.ids)), space, index.version)
        assert loaded.ids == index.ids
        path = store.root / asset.relative_path
        path.write_bytes(b"corrupted")
        with pytest.raises(ValueError):
            loaded.load(store, asset, index.ids, space, index.version)


@pytest.mark.asyncio
async def test_repeated_prepare_reuses_index_without_persistent_cache(tmp_path):
    """关闭持久缓存时仍复用本次已发布索引，检索结果与定位保持一致。"""
    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    embedder = FakeEmbedder()
    context = RagContext(
        repo,
        store,
        embedder,
        EmbeddingSpace("fixture", "m", 3),
        "cosine",
        2,
        "run",
        cache_enabled=False,
    )
    evidence = EvidenceSpan(
        evidence_id="a", block_id="b", quote="原文证据", locator=Locator(page=2)
    )
    await context.prepare("doc", [evidence])
    before = context.index.search([1, 0, 0], 2)
    version = context.index.version
    ids = context.index.vector_ids.copy()
    await context.prepare("doc", [evidence.model_copy(deep=True)])
    assert embedder.calls == 1
    assert context.index.search([1, 0, 0], 2) == before
    assert context.index.version == version and context.index.vector_ids == ids
    assert context.evidence["a"] == evidence
    # 定位变化也属于输入变化，不允许复用旧的证据映射。
    changed = evidence.model_copy(update={"locator": Locator(page=3)})
    await context.prepare("doc", [changed])
    assert embedder.calls == 2 and context.evidence["a"].locator.page == 3
    await context.prepare("other-doc", [changed])
    assert embedder.calls == 3


@pytest.mark.asyncio
async def test_reuse_restores_binding_and_does_not_cross_run(tmp_path):
    """共享实例或其他准备任务不能让运行快照指向错误的文档索引。"""
    from tc_rag.domain.models import Run
    from tc_rag.domain.pipeline import StageSnapshot

    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    embedder = FakeEmbedder()
    space = EmbeddingSpace("fixture", "m", 3)
    for run_id in ("first", "second"):
        repo.save("runs", run_id, Run(run_id=run_id, status="running", config_snapshot={}))
    context = RagContext(repo, store, embedder, space, "cosine", 2, "first", cache_enabled=False)
    evidence = EvidenceSpan(evidence_id="a", block_id="a", quote="原文", locator=Locator())
    await context.prepare("doc", [evidence])
    binding = repo.get("stage_snapshots", "first:index", StageSnapshot)
    other = RagContext(repo, store, embedder, space, "cosine", 2, "first", cache_enabled=False)
    changed = evidence.model_copy(update={"quote": "另一份文档"})
    await other.prepare("other-doc", [changed])
    assert repo.get("stage_snapshots", "first:index", StageSnapshot) != binding
    calls = embedder.calls
    await context.prepare("doc", [evidence])
    assert embedder.calls == calls
    assert repo.get("stage_snapshots", "first:index", StageSnapshot) == binding
    context.run_id = "second"
    await context.prepare("doc", [evidence])
    assert embedder.calls == calls + 1
    second = repo.get("stage_snapshots", "second:index", StageSnapshot)
    assert second is not None and binding is not None
    assert second.payload["manifest_id"] != binding.payload["manifest_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["file", "manifest"])
async def test_prepared_index_reuse_rejects_corruption(tmp_path, corrupt):
    """复用不得绕过发布文件校验或持久化 manifest 校验。"""
    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    embedder = FakeEmbedder()
    context = RagContext(
        repo,
        store,
        embedder,
        EmbeddingSpace("fixture", "m", 3),
        "cosine",
        2,
        "run",
        cache_enabled=False,
    )
    evidence = EvidenceSpan(evidence_id="a", block_id="b", quote="原文", locator=Locator())
    await context.prepare("doc", [evidence])
    if corrupt == "file":
        assert context._prepared_asset is not None
        (store.root / context._prepared_asset.relative_path).write_bytes(b"corrupted")
    else:
        assert context._prepared_manifest is not None
        manifest = context._prepared_manifest.model_copy(deep=True)
        manifest.payload["version"] = "tampered"
        repo.save("index_manifests", manifest.record_id, manifest)
    with pytest.raises(ValueError):
        await context.prepare("doc", [evidence])
    assert embedder.calls == 1


@pytest.mark.asyncio
async def test_cache_and_document_scope(tmp_path):
    repo, store = SqliteRepository(tmp_path / "db.sqlite"), LocalArtifactStore(tmp_path / "files")
    embedder = FakeEmbedder()
    space = EmbeddingSpace("fixture", "m", 3)
    service = RagContext(repo, store, embedder, space, "cosine", 2, "run")
    a = EvidenceSpan(evidence_id="a", block_id="ba", quote="甲文档原文", locator=Locator(page=1))
    b = EvidenceSpan(evidence_id="b", block_id="bb", quote="乙文档原文", locator=Locator(page=1))
    await service.prepare("hash-a", [a])
    await service.vector("query")
    calls = embedder.calls
    await service.prepare("hash-a", [a])
    result = await service.retrieve("query", {"a"})
    assert result["query_embedding_cache_hit"] and embedder.calls == calls
    assert result["direct_ids"] == ["a"] and result["supplemental_evidence"] == []
    await service.prepare("hash-b", [b])
    with pytest.raises(ValueError, match="范围"):
        await service.retrieve("query", {"a"})
    result = await service.retrieve("query", set())
    assert [h["object_id"] for h in result["hits"]] == ["b"]
    changed = RagContext(
        repo, store, embedder, EmbeddingSpace("fixture", "other", 3), "cosine", 1, "r2"
    )
    await changed.vector("query")
    assert embedder.calls > calls


@pytest.mark.asyncio
async def test_supplement_is_adopted_and_budget_omission_is_auditable(tmp_path):
    from test_pipeline import setup

    from tc_rag.domain.models import Run
    from tc_rag.domain.pipeline import CaseBatch

    pipeline, _, _, _ = setup(tmp_path)
    pipeline.active_run_id = "run_bundle"
    pipeline.repository.save(
        "runs", "run_bundle", Run(run_id="run_bundle", status="running", config_snapshot={})
    )
    retriever = RagContext(
        pipeline.repository,
        pipeline.store,
        FakeEmbedder(),
        EmbeddingSpace("fixture", "m", 3),
        "cosine",
        2,
        "run_bundle",
    )
    pipeline.retriever = retriever
    spans = [
        EvidenceSpan(evidence_id="a", block_id="a", quote="直接证据", locator=Locator()),
        EvidenceSpan(evidence_id="b", block_id="b", quote="补充业务条件" * 100, locator=Locator()),
    ]
    await retriever.prepare("hash", spans)
    request = {"system": "fixture", "evidence": [spans[0].model_dump(mode="json")]}
    await pipeline._supplement(request, {"a"}, "业务条件", CaseBatch)
    assert [e["evidence_id"] for e in request["supplemental_evidence"]] == ["b"]
    assert request["evidence"][0]["evidence_id"] == "a"
    pipeline.max_context = 10
    request = {"evidence": [spans[0].model_dump(mode="json")]}
    await pipeline._supplement(request, {"a"}, "业务条件", CaseBatch)
    assert request["supplemental_evidence"] == []
    assert pipeline.read("run_bundle", "retrieval")["bundles"][-1]["omitted_ids"] == ["b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("indexes", [[1, 0], [0, 0], [0]])
async def test_embedding_response_alignment_and_invalid_indexes(indexes):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(i + 1), 1.0]}
                    for i in indexes
                ],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    client = AsyncOpenAI(
        api_key="fake",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    records = []
    adapter = OpenAIEmbedder(Endpoint(model="m"), Runtime(), "run", records.append, client)
    try:
        if indexes != [1, 0]:
            with pytest.raises(ValueError, match="index"):
                await adapter.embed_batch(["a", "b"], EmbeddingSpace("fixture", "m", 2, False))
            assert records[0].error == "ValueError"
        else:
            result = await adapter.embed_batch(["a", "b"], EmbeddingSpace("fixture", "m", 2, False))
            assert result.vectors == [[1, 1], [2, 1]]
    finally:
        await adapter.close()
