"""RAG 编排适配器：持久化空间/缓存/manifest，限定文档版本后构建上下文。"""

import math
from dataclasses import asdict
from time import perf_counter
from typing import Any

from tc_rag.application.ports.contracts import (
    ArtifactStore,
    Embedder,
    EmbeddingSpace,
    RankedCandidate,
    Repository,
    Reranker,
)
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Asset, EvidenceSpan, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.domain.rag import RagRecord
from tc_rag.infrastructure.retrieval.index import NumpyIndex

COLLECTIONS = {
    "space": "embedding_spaces",
    "embedding": "embedding_records",
    "requirement_set": "requirement_sets",
    "manifest": "index_manifests",
    "context": "context_bundles",
}


class RagContext:
    def __init__(
        self,
        repository: Repository,
        store: ArtifactStore,
        embedder: Embedder,
        space: EmbeddingSpace,
        metric: str,
        top_k: int,
        run_id: str,
        *,
        backend: str = "numpy",
        keyword_enabled: bool = False,
        reranker: Reranker | None = None,
        rerank_top_n: int = 6,
        cache_enabled: bool = True,
    ):
        self.repository, self.store, self.embedder = repository, store, embedder
        self.space, self.top_k, self.run_id = space, top_k, run_id
        self.index = NumpyIndex(metric)
        if backend == "faiss":
            from tc_rag.infrastructure.retrieval.faiss_index import FaissIndex

            self.index = FaissIndex(metric)
        elif backend != "numpy":
            raise ValueError("未知索引后端")
        from tc_rag.infrastructure.retrieval.keyword import KeywordIndex

        self.keyword = KeywordIndex() if keyword_enabled else None
        self.reranker, self.rerank_top_n = reranker, rerank_top_n
        self.evidence: dict[str, EvidenceSpan] = {}
        self.scope = ""
        self.space_id = fingerprint(asdict(space))
        self.cache_enabled = cache_enabled
        # 同一个实例在规划和生成阶段共用已发布索引；不跨进程或跨输入跳过校验。
        self._prepared_key: str | None = None
        self._prepared_asset: Asset | None = None
        self._prepared_manifest: RagRecord | None = None

    def save(self, key: str, kind: str, payload: dict[str, Any]) -> None:
        self.repository.save(
            COLLECTIONS[kind],
            key,
            RagRecord.model_validate(dict(record_id=key, kind=kind, payload=payload)),
        )

    def cached_vector(self, text: str) -> list[float] | None:
        if not self.cache_enabled:
            return None
        key = "embedding_" + fingerprint([self.space_id, text])
        cached = self.repository.get("embedding_records", key, RagRecord)
        if cached is None:
            return None
        data = cached.payload
        vector = list(data["vector"])
        if (
            cached.kind != "embedding"
            or data["space_id"] != self.space_id
            or data["text_fingerprint"] != fingerprint(text)
            or len(vector) != self.space.dimension
            or not all(math.isfinite(v) for v in vector)
            or data.get("vector_fingerprint") != fingerprint(vector)
        ):
            raise ValueError("向量缓存空间、文本或内容校验失败")
        return vector

    def save_vector(self, text: str, vector: list[float]) -> None:
        if self.cache_enabled:
            self.save(
                "embedding_" + fingerprint([self.space_id, text]),
                "embedding",
                {
                    "space_id": self.space_id,
                    "text_fingerprint": fingerprint(text),
                    "vector": vector,
                    "vector_fingerprint": fingerprint(vector),
                },
            )

    async def vector(self, text: str) -> tuple[list[float], bool]:
        cached = self.cached_vector(text)
        if cached is not None:
            return cached, True
        batch = await self.embedder.embed_batch([text], self.space)
        # 即使是替代适配器也必须遵守数量、维度、有限数契约，索引阶段另作校验。
        if (
            batch.space != self.space
            or len(batch.vectors) != 1
            or len(batch.vectors[0]) != self.space.dimension
        ):
            raise ValueError("embedding 适配器返回不符合空间契约")
        vector = batch.vectors[0]
        if not all(math.isfinite(v) for v in vector):
            raise ValueError("向量包含非有限数")
        self.save_vector(text, vector)
        return vector, False

    async def prepare(self, document_fingerprint: str, evidence: list[Any]) -> None:
        spans = [EvidenceSpan.model_validate(e.model_dump()) for e in evidence]
        evidence_map = {e.evidence_id: e for e in spans}
        if len(evidence_map) != len(spans):
            raise ValueError("证据 ID 重复")
        prepared_key = fingerprint(
            [
                self.run_id,
                document_fingerprint,
                [e.model_dump(mode="json") for e in spans],
                self.space_id,
                self.index.metric.name,
                type(self.index).__name__,
                self.cache_enabled,
            ]
        )
        if (
            prepared_key == self._prepared_key
            # 持久缓存开启时仍执行原有向量缓存逐项校验，不绕过损坏缓存检查。
            and not self.cache_enabled
            and self._prepared_asset is not None
            and self._prepared_manifest is not None
        ):
            # 即使复用，也重新核对持久化 manifest 和索引文件；损坏必须阻断 RAG。
            manifest = self.repository.get(
                "index_manifests", self._prepared_manifest.record_id, RagRecord
            )
            if manifest != self._prepared_manifest:
                raise ValueError("已发布索引 manifest 已变化或缺失")
            candidate = type(self.index)(self.index.metric.name)
            candidate.load(
                self.store, self._prepared_asset, list(evidence_map), self.space, self.index.version
            )
            candidate.vector_ids = self.repository.allocate_vector_ids(
                self.space_id, list(evidence_map)
            )
            # 与完整准备路径一样刷新当前运行指针，避免其他实例准备后留下旧绑定。
            self._bind_index(self.scope, candidate, manifest.record_id)
            self.index = candidate
            return
        scope = fingerprint([document_fingerprint])
        self.save(self.space_id, "space", asdict(self.space))
        self.save(
            scope,
            "requirement_set",
            {
                "document_fingerprints": [document_fingerprint],
            },
        )
        # 一次收集所有缺失文本，供应商适配器按 batch_size 分批，避免每条证据一次请求。
        values = {e.quote: self.cached_vector(e.quote) for e in spans}
        missing = [text for text, vector in values.items() if vector is None]
        if missing:
            batch = await self.embedder.embed_batch(missing, self.space)
            if (
                batch.space != self.space
                or len(batch.vectors) != len(missing)
                or any(
                    len(v) != self.space.dimension or not all(math.isfinite(x) for x in v)
                    for v in batch.vectors
                )
            ):
                raise ValueError("批量 embedding 响应不符合空间契约")
            for text, vector in zip(missing, batch.vectors):
                self.save_vector(text, vector)
                values[text] = vector
        vectors = [values[e.quote] for e in spans]
        if any(v is None for v in vectors):
            raise ValueError("索引缺少向量")
        candidate = type(self.index)(self.index.metric.name)
        candidate.build(list(evidence_map), [v for v in vectors if v is not None], self.space)
        asset = candidate.publish(self.store)
        # 先重新加载并验证，再发布 manifest；失败不会留下可查询的活动记录。
        candidate.load(self.store, asset, list(evidence_map), self.space, candidate.version)
        candidate.vector_ids = self.repository.allocate_vector_ids(
            self.space_id, list(evidence_map)
        )
        manifest_id = "manifest_" + fingerprint(
            [scope, self.space_id, candidate.version, self.run_id]
        )
        manifest_payload = {
            "scope": scope,
            "space_id": self.space_id,
            "metric": candidate.metric.name,
            "version": candidate.version,
            "asset": asset.model_dump(mode="json"),
            "ids": list(evidence_map),
            "vector_ids": candidate.vector_ids,
            "backend": type(candidate).__name__,
            "run_id": self.run_id,
        }
        self.save(manifest_id, "manifest", manifest_payload)
        self._bind_index(scope, candidate, manifest_id)
        # 文件和 manifest 全部成功后才切换活动 generation，失败保留旧对象可查询。
        self.index, self.evidence, self.scope = candidate, evidence_map, scope
        if self.keyword:
            self.keyword.build({e.evidence_id: e.quote for e in spans})
        # 只有发布、回读和范围绑定均成功后才能记住可复用状态。
        self._prepared_key, self._prepared_asset = prepared_key, asset
        self._prepared_manifest = RagRecord(
            record_id=manifest_id, kind="manifest", payload=manifest_payload
        )

    def _bind_index(self, scope: str, candidate: NumpyIndex, manifest_id: str) -> None:
        """新建和复用均在文件验证后更新同一种运行快照，不引入新字段。"""
        binding = f"{self.run_id}:index"
        # 运行快照就是本次运行的活动指针；仅在完整 manifest 校验成功后提交。
        if self.repository.get("runs", self.run_id, Run) is not None:
            self.repository.save(
                "stage_snapshots",
                binding,
                StageSnapshot(
                    snapshot_id=binding,
                    run_id=self.run_id,
                    stage="index",
                    payload={
                        "requirement_set_id": scope,
                        "space_id": self.space_id,
                        "manifest_id": manifest_id,
                        "index_version": candidate.version,
                    },
                ),
            )

    async def retrieve(self, query: str, direct_ids: set[str]) -> dict[str, Any]:
        if not direct_ids <= self.evidence.keys():
            raise ValueError("直接证据不属于当前需求范围")
        started = perf_counter()
        vector, cache_hit = await self.vector(query)
        embedded = perf_counter()
        hits = self.index.search(vector, self.top_k, set(self.evidence))
        searched = perf_counter()
        vector_hits = [asdict(h) for h in hits]
        keyword_ids = (
            self.keyword.search(query, self.top_k, set(self.evidence)) if self.keyword else []
        )
        # 命中记录保留原始向量分数；混合分数单独命名，不假装是余弦相似度。
        from tc_rag.infrastructure.retrieval.keyword import rrf

        fusion = rrf([[h.object_id for h in hits], keyword_ids], self.top_k) if self.keyword else []
        selected = [key for key, _ in fusion] if self.keyword else [h.object_id for h in hits]
        rerank_event: dict[str, Any] = {"status": "disabled"}
        rerank_started = perf_counter()
        if self.reranker:
            candidates = [
                RankedCandidate(key, self.evidence[key].quote)
                for key in selected[: self.rerank_top_n]
            ]
            try:
                ranked = await self.reranker.rank(query, candidates)
                if len(ranked) != len(candidates) or {c.object_id for c in ranked} != {
                    c.object_id for c in candidates
                }:
                    raise ValueError("重排适配器改变了候选集合")
                selected = [c.object_id for c in ranked] + selected[len(candidates) :]
                rerank_event = {
                    "status": "succeeded",
                    "scores": {c.object_id: c.score for c in ranked},
                }
            except Exception as exc:
                rerank_event = {
                    "status": "fallback",
                    "error_type": type(exc).__name__,
                    "reason": "重排失败，保留召回排名",
                }
        reranked = perf_counter()
        hit_map = {h["object_id"]: h for h in vector_hits}
        final_hits = [
            {
                **hit_map.get(
                    key,
                    {
                        "object_id": key,
                        "raw_score": None,
                        "metric": "keyword",
                        "score_kind": "keyword_only",
                        "index_version": self.index.version,
                    },
                ),
                "rank": rank,
            }
            for rank, key in enumerate(selected, 1)
        ]
        result = {
            "query_id": fingerprint(query),
            "scope": self.scope,
            "space_id": self.space_id,
            "index_version": self.index.version,
            "direct_ids": sorted(direct_ids),
            "hits": final_hits,
            "vector_hits": vector_hits,
            "keyword_ids": keyword_ids,
            "fusion": [{"object_id": key, "rrf_score": score} for key, score in fusion],
            "rerank": rerank_event,
            "supplemental_evidence": [
                self.evidence[key].model_dump(mode="json")
                for key in selected
                if key not in direct_ids
            ],
            "query_embedding_cache_hit": cache_hit,
            "timings_ms": {
                "query_embedding_ms": (embedded - started) * 1000,
                "candidate_search_ms": (searched - embedded) * 1000,
                "rerank_ms": (reranked - rerank_started) * 1000,
                "context_build_ms": (perf_counter() - reranked) * 1000,
            },
            "selection": "直接证据不经检索裁剪；检索结果只作补充，不自动升级成规则依据",
        }
        self.save(
            "context_" + fingerprint([self.run_id, query, sorted(direct_ids)]), "context", result
        )
        return result
