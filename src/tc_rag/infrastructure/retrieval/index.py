"""精确索引：先限定范围再 top-k，文件校验后才可加载。"""

import io
from dataclasses import asdict

import numpy as np

from tc_rag.application.ports.contracts import ArtifactStore, EmbeddingSpace, SearchHit
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Asset
from tc_rag.infrastructure.retrieval.metrics import get_metric


class NumpyIndex:
    def __init__(self, metric: str = "cosine"):
        self.metric = get_metric(metric)
        self.ids: list[str] = []
        self.vector_ids: list[int] = []
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.version = "unbuilt"
        self.space: EmbeddingSpace | None = None

    def build(
        self, object_ids: list[str], vectors: list[list[float]], space: EmbeddingSpace
    ) -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if not object_ids:
            matrix = np.empty((0, space.dimension), dtype=np.float32)
        if (
            space.dimension <= 0
            or len(set(object_ids)) != len(object_ids)
            or matrix.shape != (len(object_ids), space.dimension)
            or not np.all(np.isfinite(matrix))
        ):
            raise ValueError("索引 ID、维度或向量数值非法")
        # 提前拒绝余弦零向量，而不是查询某个范围时才随机失败。
        if self.metric.name.startswith("cosine") and len(matrix):
            self.metric.score(matrix[0].astype(np.float64), matrix.astype(np.float64))
        self.ids, self.vectors, self.space = list(object_ids), np.ascontiguousarray(matrix), space
        self.vector_ids = list(range(len(object_ids)))
        self.version = fingerprint(
            [self.ids, self.vectors.tolist(), asdict(space), self.metric.name]
        )

    def search(
        self, vector: list[float], top_k: int, allowed_ids: set[str] | None = None
    ) -> list[SearchHit]:
        if self.space is None or top_k <= 0:
            raise ValueError("索引尚未构建或 top_k 非正")
        mask = [i for i, key in enumerate(self.ids) if allowed_ids is None or key in allowed_ids]
        scores = self.metric.score(vector, self.vectors[mask].astype(np.float64))
        # 稳定排序使并列分数可复现；结果只使用范围内候选。
        order = np.argsort(-scores if self.metric.higher_is_better else scores, kind="stable")[
            :top_k
        ]
        return [
            SearchHit(
                self.ids[mask[int(i)]],
                float(scores[i]),
                self.metric.name,
                self.metric.score_kind,
                rank,
                self.version,
            )
            for rank, i in enumerate(order, 1)
        ]

    def publish(self, store: ArtifactStore) -> Asset:
        buffer = io.BytesIO()
        np.save(buffer, self.vectors, allow_pickle=False)
        return store.put(f"indexes/{self.version}/vectors.npy", buffer.getvalue())

    def load(
        self,
        store: ArtifactStore,
        asset: Asset,
        ids: list[str],
        space: EmbeddingSpace,
        expected_version: str,
    ) -> None:
        matrix = np.load(io.BytesIO(store.read(asset)), allow_pickle=False)
        candidate = NumpyIndex(self.metric.name)
        candidate.build(ids, matrix.tolist(), space)
        if candidate.version != expected_version:
            raise ValueError("索引内容、空间或度量与 manifest 不一致")
        self.ids, self.vectors, self.space, self.version = (
            candidate.ids,
            candidate.vectors,
            candidate.space,
            candidate.version,
        )
        self.vector_ids = candidate.vector_ids
