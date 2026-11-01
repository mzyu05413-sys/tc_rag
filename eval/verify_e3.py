"""独立复核 E3：不复用实验运行器的函数，直接从向量文件重算关键结果。

用法：python eval/verify_e3.py --dataset <材料包>
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.domain.identity import fingerprint
from tc_rag.infrastructure.retrieval.index import NumpyIndex


def _space(dimension: int) -> EmbeddingSpace:
    return EmbeddingSpace(
        provider="experiment", model="cached", dimension=dimension, normalize=True
    )


def _manual_top(matrix: np.ndarray, query: np.ndarray, k: int) -> list[int]:
    """不经过项目度量实现，按定义手算余弦 cos = a·b / (|a||b|)。"""
    numerator = matrix @ query
    denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    scores = numerator / denominator
    return list(np.argsort(-scores, kind="stable")[:k])


def main() -> int:
    parser = argparse.ArgumentParser(description="独立复核 E3 关键结果")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--results", type=Path, default=None)
    args = parser.parse_args()

    matrix = np.load(args.dataset / "corpus_vectors.npy").astype(np.float64)
    queries = np.load(args.dataset / "query_vectors.npy").astype(np.float64)
    corpus = json.loads((args.dataset / "corpus.json").read_text(encoding="utf-8"))
    ids = [item["object_id"] for item in corpus]
    space = _space(matrix.shape[1])

    import faiss

    from tc_rag.infrastructure.retrieval.faiss_index import FaissIndex

    print("=" * 70)
    print("1. 手工余弦 vs 框架 NumPyIndex vs FAISS 适配器 vs FAISS 原生")
    print("=" * 70)
    numpy_index = NumpyIndex("cosine")
    numpy_index.build(ids, matrix.astype(np.float32), space)
    faiss_index = FaissIndex("cosine")
    faiss_index.build(ids, matrix.astype(np.float32), space)
    native = faiss.IndexIDMap2(faiss.IndexFlatIP(matrix.shape[1]))
    native.add_with_ids(
        np.ascontiguousarray(matrix.astype(np.float32)), np.arange(len(ids), dtype=np.int64)
    )

    for position in (0, 7, 19):
        query = queries[position]
        manual = _manual_top(matrix, query, 10)
        framework = [hit.object_id for hit in numpy_index.search(query.tolist(), 10)]
        adapter = [hit.object_id for hit in faiss_index.search(query.tolist(), 10)]
        _, native_ids = native.search(
            np.ascontiguousarray(query.astype(np.float32).reshape(1, -1)), 10
        )
        native_list = [ids[index] for index in native_ids[0]]
        manual_ids = [ids[index] for index in manual]
        print(f"\n查询 Q{position + 1:03d}")
        print(f"  手工余弦前3 : {[name[:26] for name in manual_ids[:3]]}")
        print(f"  框架 NumPy   : {[name[:26] for name in framework[:3]]}")
        print(f"  FAISS 适配器 : {[name[:26] for name in adapter[:3]]}")
        print(f"  FAISS 原生   : {[name[:26] for name in native_list[:3]]}")
        same_set = (
            len(
                {
                    frozenset(manual_ids),
                    frozenset(framework),
                    frozenset(adapter),
                    frozenset(native_list),
                }
            )
            == 1
        )
        print(f"  → 四者前10集合是否相同: {same_set}")
        print(f"  → 手工与框架顺序完全相同: {manual_ids == framework}")
        print(f"  → 手工与适配器顺序完全相同: {manual_ids == adapter}")

    print()
    print("=" * 70)
    print("2. 欧氏与余弦的恒等关系 d² = 2 - 2cos（单位向量）")
    print("=" * 70)
    query = queries[0]
    cosine = (matrix @ query) / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(query))
    squared_l2 = np.sum((matrix - query) ** 2, axis=1)
    print(f"  最大绝对误差: {np.max(np.abs(squared_l2 - (2 - 2 * cosine))):.3e}")

    print()
    print("=" * 70)
    print("3. 构建耗时是否由 version 指纹主导")
    print("=" * 70)
    started = perf_counter()
    fingerprint([ids, matrix.tolist(), asdict(space), "cosine_numpy"])
    fingerprint_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    NumpyIndex("cosine").build(ids, matrix.astype(np.float32), space)
    build_ms = (perf_counter() - started) * 1000
    print(f"  指纹计算: {fingerprint_ms:.1f} ms | 完整 build: {build_ms:.1f} ms "
          f"| 占比 {fingerprint_ms / build_ms * 100:.1f}%")

    if args.results and args.results.is_file():
        print()
        print("=" * 70)
        print("4. 结果文件自洽性：QPS 与延迟是否互相印证")
        print("=" * 70)
        payload: dict[str, Any] = json.loads(args.results.read_text(encoding="utf-8"))
        real = payload["real"]["metrics"]["cosine"]
        p50 = real["latency_ms"]["numpy"]["p50"]
        qps = real["batch_qps"]["numpy"]["16"].get("qps")
        print(f"  numpy 余弦 p50 = {p50:.2f} ms；按 1/p50 推算 QPS ≈ {1000 / p50:.1f}")
        print(f"  记录的 batch=16 QPS        = {qps:.1f}")
        print(f"  → 两者同量级说明 QPS 是顺序单查询吞吐，不是矩阵批量: "
              f"{0.5 < qps / (1000 / p50) < 1.5}")
        for metric in ("cosine", "euclidean"):
            item = payload["real"]["metrics"][metric]
            mismatched = item["consistency"]["score_mismatch_queries"]
            print(f"  {metric}: 分数不一致查询 = {mismatched or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
