"""可选 FAISS Flat 精确索引；受限查询先构造范围内索引再取 top-k。"""

import importlib

import numpy as np
from numpy.typing import NDArray

from tc_rag.application.ports.contracts import SearchHit
from tc_rag.infrastructure.retrieval.index import NumpyIndex


class FaissIndex(NumpyIndex):
    # 持久化使用应用管理的非 pickle 矩阵，校验后重建 native 索引。
    # 避免读取来源不明的 FAISS 二进制文件。
    def __init__(self, metric: str = "cosine"):
        super().__init__(metric)
        try:
            self.faiss = importlib.import_module("faiss")
        except ImportError:
            raise ValueError('faiss 后端需要安装 pip install -e ".[faiss]"') from None

    def search(
        self, vector: list[float], top_k: int, allowed_ids: set[str] | None = None
    ) -> list[SearchHit]:
        if self.space is None or top_k <= 0:
            raise ValueError("索引尚未构建或 top_k 非正")
        positions = [
            i for i, key in enumerate(self.ids) if allowed_ids is None or key in allowed_ids
        ]
        matrix = self.vectors[positions].copy()
        # 查询随后由一维转为二维；显式标注元素类型，避免新版 NumPy 固定首次推断的形状。
        query: NDArray[np.float64] = np.asarray(vector, dtype=np.float64)
        # 复用同一度量契约检查维度、有限值和余弦零向量。
        self.metric.score(query, matrix.astype(np.float64))
        if not positions:
            return []
        query = query.reshape(1, -1)
        if self.metric.name.startswith("cosine"):
            # 用 float64 稳定归一化，避免 float32 模长下溢后 FAISS 把非零向量当零。
            def unit(values: NDArray[np.float64]) -> NDArray[np.float32]:
                scaled = values / np.max(np.abs(values), axis=1, keepdims=True)
                return np.asarray(
                    scaled / np.linalg.norm(scaled, axis=1, keepdims=True), dtype=np.float32
                )

            matrix = unit(matrix.astype(np.float64))
            query = unit(query).astype(np.float64)
        query32 = np.ascontiguousarray(query, dtype=np.float32)
        if not np.all(np.isfinite(query32)):
            raise ValueError("查询超出 FAISS float32 数值范围")
        flat = (
            self.faiss.IndexFlatL2(self.space.dimension)
            if self.metric.name == "euclidean"
            else self.faiss.IndexFlatIP(self.space.dimension)
        )
        native = self.faiss.IndexIDMap2(flat)
        # SQLite 分配的 int64 ID 与业务 ID 映射可独立保存；没有映射时仅用于独立索引测试。
        native_ids = np.asarray([self.vector_ids[i] for i in positions], dtype=np.int64)
        native.add_with_ids(np.ascontiguousarray(matrix), native_ids)
        scores, ids = native.search(query32, min(top_k, len(positions)))
        if not np.all(np.isfinite(scores)) or np.any(ids == -1):
            raise ValueError("FAISS 评分溢出或候选映射缺失")
        mapping = {self.vector_ids[i]: self.ids[i] for i in positions}
        return [
            SearchHit(
                mapping[int(key)],
                float(score),
                self.metric.name,
                self.metric.score_kind,
                rank,
                self.version,
            )
            for rank, (key, score) in enumerate(zip(ids[0], scores[0]), 1)
        ]
