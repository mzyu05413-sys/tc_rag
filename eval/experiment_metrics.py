"""检索实验指标：纯函数，按每查询计算后宏平均，并保留分母与失败计数。

口径与《第六章实验开展指南》§2.7 一致：
- Recall@k：Top-k 与相关集合交集 / 该查询相关对象总数
- Hit@k：Top-k 至少命中一个相关对象
- MRR@L：首个相关对象排名倒数，未命中记 0，只在前 L 条内查找
- nDCG@k：二值相关度，增益 1/0，对数折扣
- Top-k 重叠率：与基线 Top-k 的交集 / 有效 k
"""

from collections.abc import Iterable, Sequence
from math import log2
from typing import Any

Ranked = Sequence[str]


def recall_at_k(ranked: Ranked, relevant: Iterable[str], k: int) -> float | None:
    """返回单查询 Recall@k；相关集合为空时返回 None 而不是 0。"""
    labels = set(relevant)
    if k <= 0 or not labels:
        return None
    return len(labels & set(ranked[:k])) / len(labels)


def hit_at_k(ranked: Ranked, relevant: Iterable[str], k: int) -> float | None:
    labels = set(relevant)
    if k <= 0 or not labels:
        return None
    return 1.0 if labels & set(ranked[:k]) else 0.0


def mrr_at_l(ranked: Ranked, relevant: Iterable[str], limit: int) -> float | None:
    """只在前 limit 条内查找第一个相关对象；未命中记 0。"""
    labels = set(relevant)
    if limit <= 0 or not labels:
        return None
    for rank, object_id in enumerate(ranked[:limit], 1):
        if object_id in labels:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Ranked, relevant: Iterable[str], k: int) -> float | None:
    """二值相关度下按 log2(rank+1) 折扣；理想排名为所有相关对象排在最前。"""
    labels = set(relevant)
    if k <= 0 or not labels:
        return None
    gain = sum(
        1.0 / log2(rank + 1) for rank, object_id in enumerate(ranked[:k], 1) if object_id in labels
    )
    ideal = sum(1.0 / log2(rank + 1) for rank in range(1, min(len(labels), k) + 1))
    return gain / ideal if ideal else None


def topk_overlap(ranked: Ranked, baseline: Ranked, k: int) -> tuple[float, int] | None:
    """返回 (重叠率, 有效 k)。候选不足时用两侧较小集合的长度作分母。"""
    if k <= 0:
        return None
    left, right = list(ranked[:k]), list(baseline[:k])
    effective = min(len(left), len(right))
    if effective == 0:
        return None
    return len(set(left) & set(right)) / effective, effective


def percentiles(
    samples: Sequence[float], points: Sequence[float] = (0.5, 0.95)
) -> dict[str, float]:
    """最近秩法分位数；样本为空时返回空字典，不编造数值。"""
    if not samples:
        return {}
    ordered = sorted(samples)
    result: dict[str, float] = {}
    for point in points:
        index = max(1, min(len(ordered), int(-(-len(ordered) * point // 1))))
        result[f"p{int(point * 100)}"] = ordered[index - 1]
    return result


def macro_average(values: Iterable[float | None]) -> dict[str, Any]:
    """只对非 None 求均值，同时报告有效与跳过数量。"""
    # 先材料化：入参可能是生成器，遍历两次会让跳过计数恒为 0。
    items = list(values)
    kept = [value for value in items if value is not None]
    skipped = len(items) - len(kept)
    return {
        "value": sum(kept) / len(kept) if kept else None,
        "judged": len(kept),
        "skipped": skipped,
    }
