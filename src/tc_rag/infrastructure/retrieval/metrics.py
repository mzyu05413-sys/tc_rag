"""检索度量：向量打分与索引后端分离；距离不能与相似度直接相加。"""

import math
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

Vector = list[float] | NDArray[np.float64]
Matrix = list[list[float]] | NDArray[np.float64]


class SimilarityMetric(Protocol):
    name: str
    score_kind: str
    higher_is_better: bool

    def score(self, query: Vector, matrix: Matrix) -> NDArray[np.float64]: ...


def _arrays(query: Vector, matrix: Matrix) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """不静默重塑向量；空候选用 [] 或明确的 (0,d) 矩阵表示。"""
    q = np.asarray(query, dtype=np.float64)
    m = np.asarray(matrix, dtype=np.float64)
    if q.ndim != 1 or not q.size:
        raise ValueError("查询必须为非空一维向量")
    if m.ndim == 1 and m.size == 0:
        m = np.empty((0, q.size), dtype=np.float64)
    if m.ndim != 2 or m.shape[1] != q.size:
        raise ValueError("候选必须为二维矩阵，且维度与查询一致")
    if not np.all(np.isfinite(q)) or not np.all(np.isfinite(m)):
        raise ValueError("向量不能包含 NaN 或无穷大")
    return q, m


def _unit(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """先按最大绝对值缩放再归一化，避免很大/很小的有限数溢出或下溢。"""
    scale = np.max(np.abs(values), axis=-1, keepdims=True)
    if np.any(scale == 0):
        raise ValueError("余弦相似度不接受零向量")
    scaled = values / scale
    return np.asarray(
        scaled / np.sqrt(np.sum(scaled * scaled, axis=-1, keepdims=True)), dtype=np.float64
    )


class Metric:
    """cosine_math 是逐项对照实现，其余使用 NumPy 批量计算。"""

    def __init__(self, name: Literal["cosine_math", "cosine_numpy", "euclidean", "dot"]):
        self.name: str = name
        self.score_kind = (
            "l2_squared"
            if name == "euclidean"
            else ("inner_product" if name == "dot" else "cosine_similarity")
        )
        self.higher_is_better = name != "euclidean"

    def score(self, query: Vector, matrix: Matrix) -> NDArray[np.float64]:
        q, m = _arrays(query, matrix)
        if self.name.startswith("cosine"):
            if self.name == "cosine_math":
                # 每个向量独立使用纯 math 运算，作为批量实现的数值对照。
                def unit(row: list[float]) -> list[float]:
                    scale = max(abs(x) for x in row)
                    if scale == 0:
                        raise ValueError("余弦相似度不接受零向量")
                    scaled = [x / scale for x in row]
                    norm = math.sqrt(math.fsum(x * x for x in scaled))
                    return [x / norm for x in scaled]

                math_query = unit(q.tolist())
                result = np.array(
                    [math.fsum(a * b for a, b in zip(math_query, unit(row))) for row in m.tolist()],
                    dtype=np.float64,
                )
            else:
                uq = _unit(q)
                result = _unit(m) @ uq if len(m) else np.empty(0, dtype=np.float64)
            return np.clip(result, -1.0, 1.0)
        with np.errstate(over="ignore", invalid="ignore"):
            result = np.sum((m - q) ** 2, axis=1) if self.name == "euclidean" else m @ q
        if not np.all(np.isfinite(result)):
            raise ValueError("打分结果溢出，请调整向量数值范围")
        return np.asarray(result, dtype=np.float64)


def get_metric(name: str) -> SimilarityMetric:
    """配置 cosine 默认使用批量实现；欧氏距离明确为平方距离。"""
    mapping: dict[str, Literal["cosine_math", "cosine_numpy", "euclidean", "dot"]] = {
        "cosine": "cosine_numpy",
        "cosine_numpy": "cosine_numpy",
        "cosine_math": "cosine_math",
        "euclidean": "euclidean",
        "dot": "dot",
    }
    if name not in mapping:
        raise ValueError("未知的相似度度量")
    return Metric(mapping[name])
