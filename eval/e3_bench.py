"""E3：NumPy 与 FAISS 后端对照（离线，不调用模型，不写正式数据库）。

覆盖第六章表 6-13～6-17：一致性、单查询延迟、批量吞吐、构建/加载/内存/磁盘、范围过滤。
近似索引（IVF/HNSW）当前未实现，不参与测量。
"""

import os
import platform
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import psutil

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.retrieval.faiss_index import FaissIndex
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore

TOP_K = 10
# (N, d, repeats)。10^5 使用合成维度 256 以适配本机可用内存，结果表中单独注明。
DEFAULT_PLAN = ((1000, 2048, 30), (10000, 2048, 10), (100000, 128, 5))
SKIPPED_PLAN = ((1000000, 128, "可用内存不足，未执行"),)


def _space(dimension: int) -> EmbeddingSpace:
    return EmbeddingSpace(
        provider="experiment", model="cached", dimension=dimension, normalize=True
    )


def environment() -> dict[str, Any]:
    import faiss

    memory = psutil.virtual_memory()
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "faiss": faiss.__version__,
        "cpu_logical": os.cpu_count(),
        "faiss_omp_threads": int(faiss.omp_get_max_threads()),
        "memory_total_gb": round(memory.total / 1e9, 1),
        "memory_available_gb": round(memory.available / 1e9, 2),
        "note": "未固定 BLAS/OpenMP 线程数；延迟随机器负载波动，组间比较优先于绝对值",
    }


def _percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "p50": ordered[max(0, int(len(ordered) * 0.5) - 1)],
        "p95": ordered[max(0, int(len(ordered) * 0.95) - 1)],
        "samples": len(ordered),
    }


def _search_timings(index: Any, queries: np.ndarray, repeats: int) -> list[float]:
    timings: list[float] = []
    for _ in range(repeats):
        for row in queries:
            started = perf_counter()
            index.search(np.asarray(row, dtype=np.float64).tolist(), TOP_K)
            timings.append((perf_counter() - started) * 1000)
    return timings


def _batch_qps(index: Any, queries: np.ndarray, batch: int, repeats: int) -> dict[str, Any]:
    """批量定义为循环单查询；返回真实墙钟吞吐，不冒充矩阵批量 API。"""
    if len(queries) < batch:
        return {"batch": batch, "status": "unavailable", "reason": f"查询仅 {len(queries)} 条"}
    subset = np.asarray(queries[:batch], dtype=np.float64)
    started = perf_counter()
    for _ in range(repeats):
        for row in subset:
            index.search(row.tolist(), TOP_K)
    elapsed = perf_counter() - started
    return {
        "batch": batch,
        "status": "measured",
        "qps": (batch * repeats) / elapsed if elapsed else None,
        "queries": batch * repeats,
        "wall_seconds": elapsed,
    }


def _native_index(matrix: np.ndarray, metric: str) -> Any:
    import faiss

    dimension = int(matrix.shape[1])
    flat = faiss.IndexFlatL2(dimension) if metric == "euclidean" else faiss.IndexFlatIP(dimension)
    index = faiss.IndexIDMap2(flat)
    index.add_with_ids(
        np.ascontiguousarray(matrix), np.arange(matrix.shape[0], dtype=np.int64)
    )
    return index


def _native_timings(
    matrix: np.ndarray, metric: str, queries: np.ndarray, repeats: int
) -> dict[str, Any]:
    """一次构建后反复 search 的原生内核，单独一列，不冒充框架适配器行为。"""
    import faiss

    started = perf_counter()
    index = _native_index(matrix, metric)
    build_ms = (perf_counter() - started) * 1000
    timings: list[float] = []
    for _ in range(repeats):
        for row in queries:
            query = np.ascontiguousarray(
                np.asarray(row, dtype=np.float32).reshape(1, -1)
            )
            started = perf_counter()
            index.search(query, TOP_K)
            timings.append((perf_counter() - started) * 1000)
    return {
        "build_ms": build_ms,
        "latency": _percentiles(timings),
        "threads": faiss.omp_get_max_threads(),
    }


def _build_and_load(
    cls: Any, metric: str, ids: list[str], matrix: np.ndarray, dimension: int
) -> tuple[Any, dict[str, Any]]:
    store_dir = Path(tempfile.mkdtemp(prefix="e3-index-"))
    store = LocalArtifactStore(store_dir)
    space = _space(dimension)
    started = perf_counter()
    index = cls(metric)
    index.build(ids, matrix, space)
    build_ms = (perf_counter() - started) * 1000
    asset = index.publish(store)
    disk = len(store.read(asset))
    started = perf_counter()
    loaded = cls(metric)
    loaded.load(store, asset, ids, space, index.version)
    load_ms = (perf_counter() - started) * 1000
    return index, {
        "build_ms": build_ms,
        "load_ms": load_ms,
        "matrix_bytes": int(matrix.nbytes),
        "disk_bytes": disk,
        "process_rss_bytes": int(psutil.Process().memory_info().rss),
    }


def _consistency(
    numpy_index: Any, faiss_index: Any, queries: np.ndarray, labels: list[str]
) -> dict[str, Any]:
    identical = tie_only = 0
    score_mismatch: list[str] = []
    for name, row in zip(labels, queries, strict=False):
        query = np.asarray(row, dtype=np.float64)
        left = numpy_index.search(query.tolist(), TOP_K)
        right = faiss_index.search(query.tolist(), TOP_K)
        if [hit.object_id for hit in left] == [hit.object_id for hit in right]:
            identical += 1
            continue
        left_scores = np.array([hit.raw_score for hit in left], dtype=np.float64)
        right_scores = np.array([hit.raw_score for hit in right], dtype=np.float64)
        if left_scores.shape == right_scores.shape and np.allclose(
            left_scores, right_scores, atol=1e-5
        ):
            tie_only += 1
        else:
            score_mismatch.append(name)
    return {
        "compared_queries": len(labels),
        "top10_identical": identical,
        "same_scores_different_tie_order": tie_only,
        "score_mismatch_queries": score_mismatch,
    }


def _duplicate_rows(matrix: np.ndarray) -> int:
    rows = np.unique(np.round(np.asarray(matrix, dtype=np.float64), 12), axis=0)
    return int(np.asarray(matrix).shape[0] - rows.shape[0])


def _real_block(
    dataset: Any, query_count: int, repeats: int
) -> dict[str, Any]:
    matrix = np.asarray(dataset.index_vectors, dtype=np.float32)
    ids = list(dataset.object_ids)
    dimension = int(matrix.shape[1])
    queries = np.asarray(dataset.query_vectors[:query_count], dtype=np.float64)
    queries_all = np.asarray(dataset.query_vectors, dtype=np.float64)
    labels = [item["record_id"] for item in dataset.queries[:query_count]]
    block: dict[str, Any] = {
        "rows": int(matrix.shape[0]),
        "dimension": dimension,
        "duplicate_rows": _duplicate_rows(matrix),
        "metrics": {},
    }
    for metric in ("cosine", "euclidean"):
        numpy_index, numpy_cost = _build_and_load(NumpyIndex, metric, ids, matrix, dimension)
        faiss_index, faiss_cost = _build_and_load(FaissIndex, metric, ids, matrix, dimension)
        block["metrics"][metric] = {
            "consistency": _consistency(numpy_index, faiss_index, queries, labels),
            "latency_ms": {
                "numpy": _percentiles(_search_timings(numpy_index, queries, repeats)),
                "faiss": _percentiles(_search_timings(faiss_index, queries, repeats)),
                "faiss_native": _native_timings(matrix, metric, queries, repeats),
            },
            "batch_qps": {
                "numpy": {
                    "16": _batch_qps(numpy_index, queries_all, 16, 2),
                    "128": _batch_qps(numpy_index, queries_all, 128, 1),
                },
                "faiss": {
                    "16": _batch_qps(faiss_index, queries_all, 16, 2),
                    "128": _batch_qps(faiss_index, queries_all, 128, 1),
                },
            },
            "cost": {"numpy": numpy_cost, "faiss": faiss_cost},
        }
    return block


def _scope_block(dataset: Any, query_count: int) -> dict[str, Any]:
    matrix = np.asarray(dataset.index_vectors, dtype=np.float32)
    half = max(1, len(dataset.object_ids) // 2)
    subset = set(dataset.object_ids[:half])
    rows = [index for index, key in enumerate(dataset.object_ids) if key in subset]
    scoped_ids = [dataset.object_ids[index] for index in rows]
    scoped_matrix = matrix[rows]
    dimension = int(matrix.shape[1])
    queries = np.asarray(dataset.query_vectors[:query_count], dtype=np.float64)
    result: dict[str, Any] = {"scope_size": len(scoped_ids), "metrics": {}}
    for metric in ("cosine", "euclidean"):
        numpy_index, numpy_cost = _build_and_load(
            NumpyIndex, metric, scoped_ids, scoped_matrix, dimension
        )
        faiss_index, faiss_cost = _build_and_load(
            FaissIndex, metric, scoped_ids, scoped_matrix, dimension
        )
        numpy_times: list[float] = []
        faiss_times: list[float] = []
        for row in queries:
            query = np.asarray(row, dtype=np.float64).tolist()
            started = perf_counter()
            numpy_index.search(query, TOP_K, subset)
            numpy_times.append((perf_counter() - started) * 1000)
            started = perf_counter()
            faiss_index.search(query, TOP_K, subset)
            faiss_times.append((perf_counter() - started) * 1000)
        result["metrics"][metric] = {
            "numpy_p50_ms": _percentiles(numpy_times)["p50"],
            "faiss_p50_ms": _percentiles(faiss_times)["p50"],
            "numpy_build_ms": numpy_cost["build_ms"],
            "faiss_build_ms": faiss_cost["build_ms"],
        }
    return result


def _feasible(rows: int, dimension: int) -> tuple[bool, str]:
    # NumPy 打分与 FAISS 适配器都会复制 float64 矩阵，按 4 份估算峰值。
    need = rows * dimension * 8 * 4
    available = psutil.virtual_memory().available
    if need > available * 0.8:
        return False, f"预计峰值 {need / 1e9:.1f} GB，可用 {available / 1e9:.2f} GB"
    return True, ""


def _synthetic_block(rows: int, dimension: int, repeats: int, seed: int = 42) -> dict[str, Any]:
    feasible, reason = _feasible(rows, dimension)
    if not feasible:
        return {"rows": rows, "dimension": dimension, "status": "skipped", "reason": reason}
    rng = np.random.default_rng(seed)
    matrix = rng.standard_normal((rows, dimension), dtype=np.float64)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    queries = rng.standard_normal((128, dimension), dtype=np.float64)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    ids = [f"S{index:06d}" for index in range(rows)]
    float32 = matrix.astype(np.float32)
    block: dict[str, Any] = {
        "rows": rows,
        "dimension": dimension,
        "seed": seed,
        "status": "measured",
        "note": "合成随机单位向量，仅用于性能与一致性，不代表业务召回",
        "metrics": {},
    }
    for metric in ("cosine", "euclidean"):
        numpy_index, numpy_cost = _build_and_load(NumpyIndex, metric, ids, float32, dimension)
        faiss_index, faiss_cost = _build_and_load(FaissIndex, metric, ids, float32, dimension)
        block["metrics"][metric] = {
            "consistency": _consistency(
                numpy_index, faiss_index, queries[:4], [f"q{index}" for index in range(4)]
            ),
            "latency_ms": {
                "numpy": _percentiles(_search_timings(numpy_index, queries[:4], repeats)),
                "faiss": _percentiles(_search_timings(faiss_index, queries[:4], repeats)),
                "faiss_native": _native_timings(float32, metric, queries[:4], repeats),
            },
            "batch_qps": {
                "numpy": {
                    "16": _batch_qps(numpy_index, queries, 16, 1),
                    "128": _batch_qps(numpy_index, queries, 128, 1),
                },
                "faiss": {
                    "16": _batch_qps(faiss_index, queries, 16, 1),
                    "128": _batch_qps(faiss_index, queries, 128, 1),
                },
            },
            "cost": {"numpy": numpy_cost, "faiss": faiss_cost},
        }
    return block


def run(
    dataset: Any,
    output: Path,
    plan: tuple[tuple[int, int, int], ...] = DEFAULT_PLAN,
    query_count: int = 8,
    repeats: int = 30,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "experiment": "E3",
        "dataset": str(dataset.root),
        "environment": environment(),
        "top_k": TOP_K,
        "real": _real_block(dataset, query_count, repeats),
        "scope": _scope_block(dataset, query_count),
        "synthetic": [_synthetic_block(*spec) for spec in plan],
        "not_executed": [
            {"kind": "近似索引", "detail": "IVF/HNSW 当前未实现"},
            *[
                {"rows": rows, "dimension": dimension, "reason": reason}
                for rows, dimension, reason in SKIPPED_PLAN
            ],
        ],
    }
    return payload
