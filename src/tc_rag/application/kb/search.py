"""固定活跃快照执行仅检索，不调用回答模型。"""

from collections.abc import Callable
from dataclasses import asdict
from math import isfinite
from time import perf_counter
from typing import Any, Protocol

from tc_rag.application.kb.ingest import IngestionService
from tc_rag.application.ports.contracts import RankedCandidate, Reranker
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.domain.identity import fingerprint, new_id


class KeywordRetriever(Protocol):
    def build(self, documents: dict[str, str]) -> None: ...
    def search(self, query: str, top_k: int, allowed: set[str]) -> list[str]: ...


class SearchService:
    def __init__(
        self, components: IngestionService, *, keyword: KeywordRetriever | None = None,
        fusion: Callable[..., list[tuple[str, float]]] | None = None,
        reranker: Reranker | None = None, candidate_count: int = 20,
        rerank_count: int = 6,
        mode: str = "vector",
        keyword_top_k: int = 20,
        rrf_constant: int = 60,
        rrf_vector_weight: float = 1.0,
        rerank_enabled: bool = True,
    ) -> None:
        if mode not in {"vector", "hybrid", "keyword"}:
            raise ValueError("未知检索模式")
        self.components = components
        self.keyword, self.fusion, self.reranker = keyword, fusion, reranker
        self.candidate_count = candidate_count
        self.rerank_count = rerank_count
        self.mode = mode
        self.keyword_top_k = keyword_top_k
        self.rrf_constant = rrf_constant
        self.rrf_vector_weight = rrf_vector_weight
        self.rerank_enabled = rerank_enabled
        self.progress: ProgressReporter | None = None
        if candidate_count <= 0 or rerank_count <= 0:
            raise ValueError("召回及重排候选数必须为正")
        if mode in {"hybrid", "keyword"} and keyword is None:
            raise ValueError("混合或纯关键词模式需要关键词检索器")
        if mode == "hybrid" and fusion is None:
            raise ValueError("混合模式需要融合函数")

    async def search(
        self, kb_id: str, query: str, *, top_k: int = 5,
        document_ids: set[str] | None = None, snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._search(
                kb_id, query, top_k=top_k, document_ids=document_ids, snapshot_id=snapshot_id,
            )
        except Exception as error:
            run_id = new_id("search_error")
            self.components.repository.save_result(run_id, {
                "run_id": run_id, "kb_id": kb_id, "query": query,
                "snapshot_id": snapshot_id, "status": "error",
                "error_type": type(error).__name__,
            })
            raise

    async def _search(
        self, kb_id: str, query: str, *, top_k: int,
        document_ids: set[str] | None, snapshot_id: str | None,
    ) -> dict[str, Any]:
        started = perf_counter()
        report(self.progress, ProgressEvent("stage", "kb_retrieve"))
        if not query.strip() or top_k <= 0:
            raise ValueError("问题不能为空，top_k 必须为正")
        components = self.components
        value = (
            components.repository.snapshot(kb_id, snapshot_id) if snapshot_id is not None
            else components.repository.active(kb_id)
        )
        if value is None:
            raise ValueError("知识库或请求快照尚未发布")
        snapshot = value.snapshot
        if snapshot.index_format_version != "1.0":
            raise ValueError("不支持的索引格式")
        if snapshot.embedding_space_id != fingerprint(asdict(components.space)):
            raise ValueError("查询向量空间与入库空间不一致")
        members = {d.document_id: d.version_id for d in snapshot.documents}
        document_versions = {d.version_id: d for d in snapshot.documents}
        if document_ids is not None and not document_ids.issubset(members):
            raise ValueError("文档过滤包含快照外成员")
        selected = set(members) if document_ids is None else document_ids
        versions = {members[key] for key in selected}
        blocks = {b.block_id: b for b in value.blocks}
        document_hashes = {d.version_id: d.content_sha256 for d in snapshot.documents}
        allowed = {
            chunk.chunk_id for chunk in value.chunks
            if all(blocks[s.block_id].document_id in versions for s in chunk.sources)
        }
        index = components.index_factory()
        if str(getattr(getattr(index, "metric", None), "name", "unknown")) != value.metric:
            raise ValueError("查询度量与已发布索引不一致")
        load_start = perf_counter()
        index.load(
            components.store, value.vector_asset, [c.chunk_id for c in value.chunks],
            components.space, value.index_version,
        )
        load_seconds = perf_counter() - load_start
        embedding_start = perf_counter()
        hits = []
        embedding_seconds = search_seconds = 0.0
        use_vector = self.mode != "keyword"
        if use_vector and allowed:
            batch = await components.embedder.embed_batch([query], components.space)
            if batch.space != components.space or len(batch.vectors) != 1:
                raise ValueError("查询向量响应空间或数量错误")
            embedding_seconds = perf_counter() - embedding_start
            search_start = perf_counter()
            hits = index.search(batch.vectors[0], max(top_k, self.candidate_count), allowed)
            search_seconds = perf_counter() - search_start
        chunks = {c.chunk_id: c for c in value.chunks}
        hit_map = {hit.object_id: hit for hit in hits}
        ranking = [hit.object_id for hit in hits]
        fusion_scores: dict[str, float] = {}
        fusion_start = perf_counter()
        if self.keyword is not None and allowed:
            self.keyword.build({key: chunk.text for key, chunk in chunks.items() if key in allowed})
            keyword_ids = self.keyword.search(
                query, max(top_k, self.keyword_top_k), allowed
            )
            if self.mode == "hybrid":
                assert self.fusion is not None
                fused = self.fusion(
                    [ranking, keyword_ids], max(top_k, self.candidate_count),
                    self.rrf_constant, [self.rrf_vector_weight, 1.0],
                )
                ranking = [key for key, _ in fused]
                fusion_scores = dict(fused)
            elif self.mode == "keyword":
                ranking = keyword_ids[: max(top_k, self.candidate_count)]
        fusion_seconds = perf_counter() - fusion_start
        original_ranking = list(ranking)
        rerank_scores: dict[str, float | None] = {}
        degradation = None
        rerank_start = perf_counter()
        if self.reranker is not None and self.rerank_enabled and ranking:
            report(self.progress, ProgressEvent("stage", "kb_rerank"))
            try:
                pool = ranking[:self.rerank_count]
                ranked = await self.reranker.rank(query, [
                    RankedCandidate(key, chunks[key].text) for key in pool
                ])
                ids = [candidate.object_id for candidate in ranked]
                if len(ids) != len(set(ids)) or set(ids) != set(pool):
                    raise ValueError("重排候选不完整或超出范围")
                if any(c.score is not None and not isfinite(c.score) for c in ranked):
                    raise ValueError("重排分数必须为有限值")
                ranking = ids + ranking[len(pool):]
                rerank_scores = {candidate.object_id: candidate.score for candidate in ranked}
            except Exception as error:
                degradation = type(error).__name__
        result = {
            "run_id": new_id("search"),
            "runtime": components.runtime_identity,
            "kb_id": kb_id, "snapshot_id": snapshot.snapshot_id, "query": query,
            "document_ids": sorted(selected), "top_k": top_k,
            "pre_rerank_ids": original_ranking, "rerank_degradation": degradation,
            "candidates": [{
                "chunk_id": key, "rank": rank,
                "raw_score": hit_map[key].raw_score if key in hit_map else None,
                "vector_rank": hit_map[key].rank if key in hit_map else None,
                "fusion_score": fusion_scores.get(key), "rerank_score": rerank_scores.get(key),
                "metric": value.metric,
                "score_kind": hit_map[key].score_kind if key in hit_map else None,
                "text": chunks[key].text,
                "sources": [{
                    **source.model_dump(),
                    "document_version": blocks[source.block_id].document_id,
                    "document_id": (
                        document_versions[blocks[source.block_id].document_id].document_id
                    ),
                    "product_version": (
                        document_versions[blocks[source.block_id].document_id].product_version
                    ),
                    "content_sha256": document_hashes[blocks[source.block_id].document_id],
                    "original_block_id": value.original_block_ids.get(source.block_id, (
                        f"doc_{document_hashes[blocks[source.block_id].document_id]}"
                        f"_block_{blocks[source.block_id].order}"
                    )),
                    "origin": blocks[source.block_id].origin,
                    "processing_model": blocks[source.block_id].processing_model,
                    "locator": blocks[source.block_id].locator.model_dump(),
                    "heading_path": blocks[source.block_id].heading_path,
                } for source in chunks[key].sources],
            } for rank, key in enumerate(ranking[:top_k], 1)],
            "timings": {"load_seconds": load_seconds, "embedding_seconds": embedding_seconds,
                        "search_seconds": search_seconds,
                        "keyword_fusion_seconds": fusion_seconds,
                        "rerank_seconds": perf_counter() - rerank_start,
                        "total_seconds": perf_counter() - started},
        }
        components.repository.save_result(str(result["run_id"]), result)
        return result
