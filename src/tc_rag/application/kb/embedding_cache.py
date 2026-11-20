"""以文本和向量空间指纹复用向量，缓存损坏时明确拒绝。"""

import math
from dataclasses import asdict

from tc_rag.application.ports.contracts import Embedder, EmbeddingBatch, EmbeddingSpace
from tc_rag.application.ports.kb import KnowledgeRepository
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Usage


class CachedEmbedder:
    def __init__(self, delegate: Embedder, repository: KnowledgeRepository) -> None:
        self.delegate, self.repository = delegate, repository
        self.capabilities = delegate.capabilities

    async def embed_batch(self, texts: list[str], space: EmbeddingSpace) -> EmbeddingBatch:
        space_id = fingerprint(asdict(space))
        keys = [fingerprint([space_id, text]) for text in texts]
        vectors: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts):
            cached = self.repository.cached_embedding(key)
            if cached is None:
                missing[key] = text
                continue
            vector = cached["vector"]
            if (
                cached.get("space_id") != space_id
                or cached.get("text_hash") != fingerprint(text)
                or cached.get("vector_hash") != fingerprint(vector)
                or len(vector) != space.dimension
                or any(not math.isfinite(value) for value in vector)
            ):
                raise ValueError("向量缓存身份或内容损坏")
            vectors[key] = vector
        usage = Usage()
        if missing:
            batch = await self.delegate.embed_batch(list(missing.values()), space)
            if batch.space != space or len(batch.vectors) != len(missing):
                raise ValueError("向量响应空间或行数不匹配")
            usage = batch.usage
            for (key, text), vector in zip(missing.items(), batch.vectors):
                if len(vector) != space.dimension or any(not math.isfinite(v) for v in vector):
                    raise ValueError("向量响应维度或数值非法")
                stored = self.repository.cache_embedding(key, {
                    "space_id": space_id, "text_hash": fingerprint(text),
                    "vector_hash": fingerprint(vector), "vector": vector,
                })
                canonical = stored["vector"]
                if (
                    stored.get("vector_hash") != fingerprint(canonical)
                    or stored.get("space_id") != space_id
                    or stored.get("text_hash") != fingerprint(text)
                    or len(canonical) != space.dimension
                    or any(not math.isfinite(value) for value in canonical)
                ):
                    raise ValueError("并发写入的向量缓存无效")
                vectors[key] = canonical
        return EmbeddingBatch(vectors=[vectors[key] for key in keys], space=space, usage=usage)
