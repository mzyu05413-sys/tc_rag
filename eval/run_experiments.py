"""第六章检索实验 E2/E3/E5：离线复算排名与指标，默认不调用任何模型。

用法（在项目根目录）：
    python eval/run_experiments.py e2 --dataset <材料包> --labels <labels.json> --output <目录>
    python eval/run_experiments.py e3 --dataset <材料包> --output <目录>
    python eval/run_experiments.py e5 --dataset <材料包> --labels <labels.json> --output <目录>
"""

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
from experiment_metrics import (
    hit_at_k,
    macro_average,
    mrr_at_l,
    ndcg_at_k,
    percentiles,
    recall_at_k,
    topk_overlap,
)

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.retrieval.metrics import get_metric

KS = (1, 3, 5, 10)
GROUPS = (
    ("C1", "cosine", True),
    ("C2", "cosine", False),
    ("C3", "euclidean", True),
    ("C4", "euclidean", False),
    ("C5", "dot", True),
    ("C6", "dot", False),
)


@dataclass
class Dataset:
    root: Path
    corpus: list[dict[str, Any]]
    queries: list[dict[str, Any]]
    object_ids: list[str]
    corpus_of_object: dict[str, str]
    object_of_corpus: dict[str, str]
    index_vectors: np.ndarray
    cached_vectors: np.ndarray
    query_vectors: np.ndarray
    raw_corpus: np.ndarray | None = None
    raw_queries: np.ndarray | None = None
    manifest: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def load_dataset(root: Path) -> Dataset:
    corpus = _read_json(root / "corpus.json")
    queries = _read_json(root / "queries.json")
    manifest = _read_json(root / "manifest.json")
    object_ids = [item["object_id"] for item in corpus]
    corpus_of_object = {item["object_id"]: item["corpus_id"] for item in corpus}
    object_of_corpus = {item["corpus_id"]: item["object_id"] for item in corpus}
    raw_dir = root / "raw"
    raw_corpus = raw_queries = None
    if (raw_dir / "corpus_raw.npy").is_file() and (raw_dir / "query_raw.npy").is_file():
        raw_corpus = np.load(raw_dir / "corpus_raw.npy")
        raw_queries = np.load(raw_dir / "query_raw.npy")
    dataset = Dataset(
        root=root,
        corpus=corpus,
        queries=queries,
        object_ids=object_ids,
        corpus_of_object=corpus_of_object,
        object_of_corpus=object_of_corpus,
        index_vectors=np.load(root / "corpus_vectors.npy"),
        cached_vectors=np.load(root / "corpus_vectors_cached.npy"),
        query_vectors=np.load(root / "query_vectors.npy"),
        raw_corpus=raw_corpus,
        raw_queries=raw_queries,
        manifest=manifest,
    )
    _check_shapes(dataset)
    return dataset


def _check_shapes(dataset: Dataset) -> None:
    rows = len(dataset.corpus)
    if dataset.index_vectors.shape[0] != rows or dataset.cached_vectors.shape[0] != rows:
        raise ValueError("语料向量行数与 corpus.json 不一致")
    if dataset.query_vectors.shape[0] != len(dataset.queries):
        raise ValueError("查询向量行数与 queries.json 不一致")
    if dataset.raw_corpus is not None and dataset.raw_corpus.shape[0] != rows:
        raise ValueError("补采原始语料向量行数与 corpus.json 不一致")
    if dataset.raw_queries is not None and dataset.raw_queries.shape[0] != len(dataset.queries):
        raise ValueError("补采原始查询向量行数与 queries.json 不一致")


def load_labels(path: Path | None, dataset: Dataset) -> dict[str, set[str] | None]:
    """返回 record_id → 相关语料 ID 集合；None 表示未标注，空集合表示已确认无相关。"""
    if path is None:
        return {item["record_id"]: None for item in dataset.queries}
    payload = _read_json(path)
    labels: dict[str, set[str] | None] = {}
    for item in payload.get("queries", []):
        record_id = item["record_id"]
        relevant = item.get("relevant_corpus_ids")
        if relevant is None:
            labels[record_id] = None
            continue
        unknown = set(relevant) - set(dataset.object_of_corpus)
        if unknown:
            raise ValueError(f"{record_id} 标注了不存在的语料 ID：{sorted(unknown)[:3]}")
        labels[record_id] = set(relevant)
    return labels


def selected_queries(
    dataset: Dataset, labels: dict[str, set[str] | None], limit_per_stage: int | None
) -> list[tuple[int, dict[str, Any]]]:
    """按 labels 选定的查询；未提供 labels 时取全部。可选每阶段上限。"""
    chosen = [(i, q) for i, q in enumerate(dataset.queries) if q["record_id"] in labels]
    if limit_per_stage is None:
        return chosen
    per_stage: dict[str, int] = {}
    result = []
    for index, query in chosen:
        stage = str(query.get("stage", "unknown"))
        if per_stage.get(stage, 0) >= limit_per_stage:
            continue
        per_stage[stage] = per_stage.get(stage, 0) + 1
        result.append((index, query))
    return result


def evaluate_rankings(
    rankings: dict[str, list[str]],
    labels: dict[str, set[str] | None],
    dataset: Dataset,
) -> dict[str, Any]:
    """按语料 ID 计算全部指标；未标注查询保持 None 并在 skipped 中计数。"""
    per_query: dict[str, dict[str, float | None]] = {}
    for record_id, ranked_objects in rankings.items():
        relevant_objects = labels.get(record_id)
        if relevant_objects is None:
            per_query[record_id] = {name: None for name in _metric_names()}
            continue
        relevant = {dataset.object_of_corpus[cid] for cid in relevant_objects}
        item: dict[str, float | None] = {}
        for k in KS:
            item[f"recall@{k}"] = recall_at_k(ranked_objects, relevant, k)
        item["hit@5"] = hit_at_k(ranked_objects, relevant, 5)
        item["mrr@10"] = mrr_at_l(ranked_objects, relevant, 10)
        item["ndcg@5"] = ndcg_at_k(ranked_objects, relevant, 5)
        per_query[record_id] = item
    summary = {
        name: macro_average(values.get(name) for values in per_query.values())
        for name in _metric_names()
    }
    return {"summary": summary, "per_query": per_query}


def _metric_names() -> list[str]:
    return [*(f"recall@{k}" for k in KS), "hit@5", "mrr@10", "ndcg@5"]


def _space(dimension: int, normalize: bool) -> EmbeddingSpace:
    return EmbeddingSpace(
        provider="experiment", model="cached", dimension=dimension, normalize=normalize
    )


def _search_rankings(
    index: Any,
    vectors: np.ndarray,
    chosen: list[tuple[int, dict[str, Any]]],
    top_k: int,
) -> tuple[dict[str, list[str]], list[float], list[list[float]], float]:
    """返回排名、逐查询耗时、逐查询原始分数，以及整轮墙钟耗时。"""
    rankings: dict[str, list[str]] = {}
    timings: list[float] = []
    scores: list[list[float]] = []
    wall_started = perf_counter()
    for position, query in chosen:
        vector = np.asarray(vectors[position], dtype=np.float64)
        started = perf_counter()
        hits = index.search(vector.tolist(), top_k)
        timings.append((perf_counter() - started) * 1000)
        rankings[query["record_id"]] = [hit.object_id for hit in hits]
        scores.append([float(hit.raw_score) for hit in hits])
    return rankings, timings, scores, perf_counter() - wall_started


def _tie_stats(scores: list[list[float]], tolerance: float = 1e-9) -> dict[str, Any]:
    """统计 Top-k 内部相邻同分的次数，用于解释并列与不同后端的顺序差异。"""
    queries_with_ties = 0
    adjacent_pairs = 0
    max_tie_group = 0
    for row in scores:
        tied = False
        group = 1
        for left, right in zip(row, row[1:]):
            if abs(left - right) <= tolerance:
                adjacent_pairs += 1
                group += 1
                tied = True
                max_tie_group = max(max_tie_group, group)
            else:
                group = 1
        if tied:
            queries_with_ties += 1
    return {
        "queries": len(scores),
        "queries_with_ties": queries_with_ties,
        "adjacent_tie_pairs": adjacent_pairs,
        "largest_tie_group": max_tie_group,
        "tolerance": tolerance,
    }


def _unit(matrix: np.ndarray) -> np.ndarray:
    values = np.asarray(matrix, dtype=np.float64)
    scale = np.max(np.abs(values), axis=1, keepdims=True)
    return values / scale / np.linalg.norm(values / scale, axis=1, keepdims=True)


def run_e2(dataset: Dataset, labels: dict[str, set[str] | None], output: Path) -> dict[str, Any]:
    chosen = selected_queries(dataset, labels, None)
    if not chosen:
        raise ValueError("没有可用查询")
    # 固定一套基准向量，只改变「是否外部归一化」这一个变量：
    # 若缓存与补采混用，重采样波动会与归一化开关混在一起无法归因。
    if dataset.raw_corpus is not None and dataset.raw_queries is not None:
        base_corpus = np.asarray(dataset.raw_corpus, dtype=np.float64)
        base_queries = np.asarray(dataset.raw_queries, dtype=np.float64)
        base_source = "resampled_raw"
    else:
        base_corpus = np.asarray(dataset.cached_vectors, dtype=np.float64)
        base_queries = np.asarray(dataset.query_vectors, dtype=np.float64)
        base_source = "cached_normalized"
    groups: list[dict[str, Any]] = []
    reference: dict[str, list[str]] = {}
    for group_id, metric_name, normalize in GROUPS:
        corpus_vectors = _unit(base_corpus) if normalize else base_corpus
        query_vectors = _unit(base_queries) if normalize else base_queries
        index = NumpyIndex(metric_name)
        index.build(
            dataset.object_ids,
            np.asarray(corpus_vectors, dtype=np.float64).tolist(),
            _space(corpus_vectors.shape[1], normalize),
        )
        rankings, timings, score_rows, wall_seconds = _search_rankings(
            index, query_vectors, chosen, 10
        )
        if group_id == "C1":
            reference = rankings
        overlaps = []
        effective_ks = []
        for record_id, ranked in rankings.items():
            pair = topk_overlap(ranked, reference.get(record_id, []), 5)
            if pair is not None:
                overlaps.append(pair[0])
                effective_ks.append(pair[1])
        metrics = evaluate_rankings(rankings, labels, dataset)
        groups.append(
            {
                "group": group_id,
                "metric": metric_name,
                "normalize": normalize,
                "status": "done",
                "vector_source": base_source,
                "vectors_normalized_in_group": normalize,
                "recall": {
                    f"@{k}": metrics["summary"][f"recall@{k}"] for k in KS
                },
                "hit@5": metrics["summary"]["hit@5"],
                "mrr@10": metrics["summary"]["mrr@10"],
                "ndcg@5": metrics["summary"]["ndcg@5"],
                "top5_overlap_vs_C1": {
                    "value": sum(overlaps) / len(overlaps) if overlaps else None,
                    "effective_k": min(effective_ks) if effective_ks else None,
                    "judged": len(overlaps),
                },
                "latency_ms": {
                    **percentiles(timings),
                    "samples": len(timings),
                    "unit": "single query search, adapter path",
                },
                "batch_qps": {
                    "value": len(chosen) / wall_seconds if wall_seconds > 0 else None,
                    "queries": len(chosen),
                    "wall_seconds": wall_seconds,
                    "note": "顺序执行全部查询的墙钟吞吐，不含 embedding 与重排",
                },
                "ties": _tie_stats(score_rows),
                "rankings": rankings,
            }
        )
    return {
        "experiment": "E2",
        "dataset": str(dataset.root),
        "base_vectors": base_source,
        "queries": [query["record_id"] for _, query in chosen],
        "top_k": 10,
        "groups": groups,
        "correctness": _e2_correctness(dataset, chosen, base_corpus, base_queries),
        "norm_check": _norm_check(dataset),
        "normalization_preprocess_ms": _normalization_cost(dataset),
    }


def _norm_check(dataset: Dataset) -> dict[str, Any]:
    """模型原始向量的 L2 范数分布；补采缺失时如实标注未取得。"""
    if dataset.raw_corpus is None or dataset.raw_queries is None:
        return {"status": "not_available", "reason": "无补采原始向量"}
    result: dict[str, Any] = {"status": "measured", "sets": {}}
    for name, matrix in (("corpus", dataset.raw_corpus), ("query", dataset.raw_queries)):
        norms = np.linalg.norm(np.asarray(matrix, dtype=np.float64), axis=1)
        result["sets"][name] = {
            "count": int(norms.size),
            "min": float(norms.min()),
            "mean": float(norms.mean()),
            "max": float(norms.max()),
            "std": float(norms.std()),
            "deviating_1pct": int(np.sum(np.abs(norms - 1.0) > 0.01)),
            "zero_vectors": int(np.sum(norms == 0)),
        }
    return result


def _normalization_cost(dataset: Dataset) -> dict[str, Any]:
    if dataset.raw_corpus is None:
        return {"status": "not_applicable", "reason": "无原始向量；C1/C3/C5 直接使用已归一化缓存"}
    corpus = np.asarray(dataset.raw_corpus, dtype=np.float64)
    started = perf_counter()
    _unit(corpus)
    return {
        "status": "measured",
        "rows": int(corpus.shape[0]),
        "dimension": int(corpus.shape[1]),
        "elapsed_ms": (perf_counter() - started) * 1000,
    }


def _e2_correctness(
    dataset: Dataset,
    chosen: list[tuple[int, dict[str, Any]]],
    base_corpus: np.ndarray,
    base_queries: np.ndarray,
) -> dict[str, Any]:
    unit_matrix = _unit(base_corpus)
    valid = [position for position, _ in chosen if position < len(base_queries)]
    if not valid:
        return {}
    math_metric = get_metric("cosine_math")
    numpy_metric = get_metric("cosine_numpy")
    euclidean_metric = get_metric("euclidean")
    max_math_diff = 0.0
    max_l2_error = 0.0
    top5_identical = 0
    top10_identical = 0
    indexes = {}
    for metric_name in ("cosine", "euclidean", "dot"):
        index = NumpyIndex(metric_name)
        index.build(dataset.object_ids, unit_matrix.tolist(), _space(unit_matrix.shape[1], True))
        indexes[metric_name] = index
    for position in valid:
        query = _unit(np.asarray(base_queries[position : position + 1], dtype=np.float64))[0]
        math_scores = math_metric.score(query, unit_matrix)
        numpy_scores = numpy_metric.score(query, unit_matrix)
        max_math_diff = max(max_math_diff, float(np.max(np.abs(math_scores - numpy_scores))))
        l2 = euclidean_metric.score(query, unit_matrix)
        max_l2_error = max(max_l2_error, float(np.max(np.abs(l2 - (2.0 - 2.0 * numpy_scores)))))
        math_top5 = set(np.argsort(-math_scores, kind="stable")[:5].tolist())
        numpy_top5 = set(np.argsort(-numpy_scores, kind="stable")[:5].tolist())
        top5_identical += int(math_top5 == numpy_top5)
        tops = {
            name: [hit.object_id for hit in index.search(query.tolist(), 10)]
            for name, index in indexes.items()
        }
        top10_identical += int(len({tuple(value) for value in tops.values()}) == 1)
    return {
        "queries_compared": len(valid),
        "cosine_math_vs_numpy_top5_identical": top5_identical == len(valid),
        "cosine_math_vs_numpy_top5_identical_queries": top5_identical,
        "cosine_math_vs_numpy_max_abs_diff": max_math_diff,
        "squared_l2_equals_2_minus_2cos_max_abs_error": max_l2_error,
        "unit_vector_top5_identical": top10_identical == len(valid),
        "unit_vector_top10_identical_queries": top10_identical,
        "note": "单位向量上三种度量应产生同一排序；不一致属实现缺陷",
    }


def run_e1(dataset: Dataset, output: Path) -> dict[str, Any]:
    """E1：检查模型原始输出向量的 L2 范数；归一化缓存无法反推原始模长。"""
    if dataset.raw_corpus is None or dataset.raw_queries is None:
        return {
            "experiment": "E1",
            "dataset": str(dataset.root),
            "status": "not_available",
            "reason": "缺少补采原始向量（dataset/raw/），归一化缓存反推不出原始模长",
        }
    raw_manifest: dict[str, Any] = {}
    manifest_path = dataset.root / "raw" / "manifest.json"
    if manifest_path.is_file():
        raw_manifest = _read_json(manifest_path)
    sets: dict[str, Any] = {}
    norms_out: dict[str, list[float]] = {}
    for name, matrix in (("corpus", dataset.raw_corpus), ("query", dataset.raw_queries)):
        values = np.asarray(matrix, dtype=np.float64)
        norms = np.linalg.norm(values, axis=1)
        norms_out[name] = [float(value) for value in norms]
        deviation = np.abs(norms - 1.0) > 0.01
        sets[name] = {
            "count": int(norms.size),
            "dimension": int(values.shape[1]),
            "min": float(norms.min()),
            "mean": float(norms.mean()),
            "max": float(norms.max()),
            "std": float(norms.std()),
            "deviating_1pct": int(np.sum(deviation)),
            "deviating_1pct_ratio": float(np.mean(deviation)),
            "zero_vectors": int(np.sum(norms == 0)),
            "non_finite": int(np.sum(~np.isfinite(values))),
        }
    return {
        "experiment": "E1",
        "dataset": str(dataset.root),
        "status": "measured",
        "vector_origin": raw_manifest.get("vector_origin"),
        "normalize": raw_manifest.get("normalize"),
        "model": raw_manifest.get("model"),
        "template_version": raw_manifest.get("template_version"),
        "request_calls": (raw_manifest.get("usage") or {}).get("calls"),
        "sets": sets,
        "norms": norms_out,
        "note": "normalize=false 请求得到的是服务端原始输出；客户端在该空间不做归一化",
    }


def run_e5(dataset: Dataset, labels: dict[str, set[str] | None], output: Path) -> dict[str, Any]:
    history = _read_json(dataset.root / "rerank_history.json")
    bundles = {item["query_id"]: item for item in history}
    chosen = selected_queries(dataset, labels, None)
    before_rankings: dict[str, list[str]] = {}
    after_rankings: dict[str, list[str]] = {}
    status: dict[str, int] = {}
    rerank_ms: list[float] = []
    mismatched: list[str] = []
    candidate_counts: set[int] = set()
    for _, query in chosen:
        bundle = bundles.get(query["query_id"])
        if bundle is None:
            continue
        before = [
            item["object_id"] for item in sorted(bundle["vector_hits"], key=lambda h: h["rank"])
        ]
        after = [item["object_id"] for item in sorted(bundle["hits"], key=lambda h: h["rank"])]
        candidate_counts.add(len(before))
        if set(before) != set(after):
            mismatched.append(query["record_id"])
        status_value = str(bundle.get("rerank", {}).get("status"))
        status[status_value] = status.get(status_value, 0) + 1
        timing = bundle.get("timings_ms", {}).get("rerank_ms")
        if timing:
            rerank_ms.append(float(timing))
        before_rankings[query["record_id"]] = before
        after_rankings[query["record_id"]] = after
    before_metrics = evaluate_rankings(before_rankings, labels, dataset)
    after_metrics = evaluate_rankings(after_rankings, labels, dataset)
    movements = _movements(before_rankings, after_rankings, labels, dataset)
    return {
        "experiment": "E5",
        "dataset": str(dataset.root),
        "queries": sorted(before_rankings),
        "candidates_per_query": sorted(candidate_counts),
        "rerank_top_n": _inferred_rerank_top_n(history),
        "rerank_status": status,
        "candidate_set_changed": mismatched,
        "before": before_metrics,
        "after": after_metrics,
        "movements": movements,
        "rank_changes": _rank_changes(before_rankings, after_rankings),
        "rerank_latency_ms": {**percentiles(rerank_ms), "samples": len(rerank_ms)},
        "note": "历史配置：召回 20 条、仅前 N 条参与重排；R0-50/R2 未执行",
    }


def _inferred_rerank_top_n(history: list[dict[str, Any]]) -> int | None:
    """从重排分数条数反推参与重排的前 N 条；配置本身不在材料包里。"""
    counts = {
        len(item["rerank"]["scores"])
        for item in history
        if item.get("rerank", {}).get("status") == "succeeded"
        and isinstance(item["rerank"].get("scores"), dict)
    }
    return counts.pop() if len(counts) == 1 else None


def _rank_changes(
    before: dict[str, list[str]], after: dict[str, list[str]]
) -> dict[str, Any]:
    """不依赖人工标注的排名变化：考察重排究竟动了多少位置。"""
    moved_items = 0
    changed_top5_slots = 0
    unchanged_queries = 0
    per_query = []
    for record_id, ranked_before in before.items():
        ranked_after = after.get(record_id, [])
        position_before = {object_id: rank for rank, object_id in enumerate(ranked_before, 1)}
        moved = sum(
            1
            for rank, object_id in enumerate(ranked_after, 1)
            if position_before.get(object_id) != rank
        )
        slots = sum(
            1
            for index in range(min(5, len(ranked_before), len(ranked_after)))
            if ranked_before[index] != ranked_after[index]
        )
        moved_items += moved
        changed_top5_slots += slots
        if not moved:
            unchanged_queries += 1
        per_query.append(
            {"record_id": record_id, "moved_items": moved, "changed_top5_slots": slots}
        )
    total = len(before)
    return {
        "queries": total,
        "moved_items_total": moved_items,
        "moved_items_mean": moved_items / total if total else None,
        "changed_top5_slots_total": changed_top5_slots,
        "changed_top5_slots_mean": changed_top5_slots / total if total else None,
        "queries_unchanged": unchanged_queries,
        "per_query": per_query,
    }


def _movements(
    before: dict[str, list[str]],
    after: dict[str, list[str]],
    labels: dict[str, set[str] | None],
    dataset: Dataset,
) -> dict[str, Any]:
    entered = left = improved = worsened = unchanged = 0
    details = []
    for record_id, ranked_before in before.items():
        relevant_objects = labels.get(record_id)
        if relevant_objects is None:
            continue
        relevant = {dataset.object_of_corpus[cid] for cid in relevant_objects}
        top_before = [object_id for object_id in ranked_before[:5] if object_id in relevant]
        top_after = [object_id for object_id in after[record_id][:5] if object_id in relevant]
        entered += len(set(top_after) - set(top_before))
        left += len(set(top_before) - set(top_after))
        rank_before = _best_rank(ranked_before, relevant)
        rank_after = _best_rank(after[record_id], relevant)
        if rank_before is None or rank_after is None:
            continue
        if rank_after < rank_before:
            improved += 1
        elif rank_after > rank_before:
            worsened += 1
        else:
            unchanged += 1
        details.append(
            {
                "record_id": record_id,
                "best_rank_before": rank_before,
                "best_rank_after": rank_after,
            }
        )
    return {
        "relevant_entered_top5": entered,
        "relevant_left_top5": left,
        "improved_queries": improved,
        "worsened_queries": worsened,
        "unchanged_queries": unchanged,
        "per_query": details,
    }


def _best_rank(ranked: list[str], relevant: set[str]) -> int | None:
    for rank, object_id in enumerate(ranked, 1):
        if object_id in relevant:
            return rank
    return None


def _write(output: Path, name: str, payload: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    print(target)


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(type(value).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="第六章检索实验 E2/E3/E5（默认不调用模型）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("e2", "e5"):
        item = sub.add_parser(name)
        item.add_argument("--dataset", required=True, type=Path)
        item.add_argument("--labels", type=Path)
        item.add_argument("--output", required=True, type=Path)
    e3 = sub.add_parser("e3")
    e3.add_argument("--dataset", required=True, type=Path)
    e3.add_argument("--output", required=True, type=Path)
    e3.add_argument("--queries", type=int, default=8)
    e3.add_argument("--repeats", type=int, default=30)
    e1 = sub.add_parser("e1")
    e1.add_argument("--dataset", required=True, type=Path)
    e1.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    if args.command == "e1":
        payload = run_e1(dataset, args.output)
        _write(args.output, "e1.json", payload)
        return 0
    if args.command == "e3":
        import e3_bench

        payload = e3_bench.run(dataset, args.output, query_count=args.queries, repeats=args.repeats)
        _write(args.output, "e3.json", payload)
        return 0
    labels = load_labels(args.labels, dataset)
    runner = run_e2 if args.command == "e2" else run_e5
    payload = runner(dataset, labels, args.output)
    _write(args.output, f"{args.command}.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
