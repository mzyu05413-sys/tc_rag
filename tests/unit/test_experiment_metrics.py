"""离线实验指标库：用已知答案固定口径，防止分母与分位数算法漂移。"""

import math

import pytest
from experiment_metrics import (
    hit_at_k,
    macro_average,
    mrr_at_l,
    ndcg_at_k,
    percentiles,
    recall_at_k,
    topk_overlap,
)


def test_recall_counts_intersection_over_all_relevant_objects():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"a", "d", "z"}, 2) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, {"a", "d", "z"}, 4) == pytest.approx(2 / 3)
    # 相关对象为空或 k 非正不是 0 召回，而是无法计算。
    assert recall_at_k(ranked, set(), 5) is None
    assert recall_at_k(ranked, {"a"}, 0) is None


def test_recall_with_k_larger_than_candidates_uses_available_only():
    assert recall_at_k(["a"], {"a", "b"}, 10) == pytest.approx(0.5)


def test_hit_at_k_is_binary_per_query():
    assert hit_at_k(["x", "a"], {"a"}, 2) == 1.0
    assert hit_at_k(["x", "a"], {"a"}, 1) == 0.0
    assert hit_at_k(["x"], set(), 5) is None


def test_mrr_uses_reciprocal_rank_and_zero_when_missing():
    assert mrr_at_l(["a", "b"], {"a"}, 10) == 1.0
    assert mrr_at_l(["x", "a"], {"a"}, 10) == 0.5
    assert mrr_at_l(["x", "y"], {"a"}, 10) == 0.0
    # 截断后未命中仍记 0，不能因为超出 L 而返回 None。
    assert mrr_at_l(["x", "a"], {"a"}, 1) == 0.0
    assert mrr_at_l(["a"], set(), 10) is None


def test_ndcg_perfect_ranking_is_one_and_partial_follows_log_discount():
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 5) == pytest.approx(1.0)
    # 相关对象排在第二位：增益 1/log2(3)，理想排名为第一位 1/log2(2)=1。
    assert ndcg_at_k(["x", "a"], {"a"}, 5) == pytest.approx(1 / math.log2(3))
    assert ndcg_at_k(["x", "y"], {"a"}, 5) == 0.0


def test_topk_overlap_reports_effective_k_when_candidates_are_short():
    value, effective = topk_overlap(["a", "b", "c"], ["a", "z"], 5)
    assert effective == 2
    assert value == pytest.approx(0.5)


def test_percentiles_use_nearest_rank_and_stay_empty_without_samples():
    ordered = [10.0, 20.0, 30.0, 40.0]
    assert percentiles(ordered) == {"p50": 20.0, "p95": 40.0}
    assert percentiles([]) == {}
    assert percentiles([7.0]) == {"p50": 7.0, "p95": 7.0}


def test_macro_average_skips_unlabeled_and_reports_counts():
    result = macro_average([1.0, None, 0.0])
    assert result == {"value": 0.5, "judged": 2, "skipped": 1}
    assert macro_average([None]) == {"value": None, "judged": 0, "skipped": 1}
