"""独立于模型的检索评分，待审核和不完整标签不得冒充完整金标。"""

import random
from math import log2
from typing import Any


def score_retrieval(
    result: dict[str, Any], gold: dict[str, Any], *, overlap_threshold: float = 0.5,
) -> dict[str, Any]:
    if gold.get("review_status") != "reviewed":
        raise ValueError("仅接受 reviewed 人工金标")
    if not 0 < overlap_threshold <= 1:
        raise ValueError("重叠阈值必须位于 (0,1]")
    groups = gold.get("evidence_groups")
    if not isinstance(groups, list):
        raise ValueError("证据集合未知")
    k = result["top_k"]
    if not isinstance(k, int) or k <= 0:
        raise ValueError("非法 k")
    candidates = result["candidates"][:k]
    found: set[int] = set()
    relevant_candidates = 0
    first_rank = None
    for rank, candidate in enumerate(candidates, 1):
        matched = set()
        for group_no, alternatives in enumerate(groups):
            if not isinstance(alternatives, list) or not alternatives:
                raise ValueError("必要证据单元须包含至少一个替代区间")
            for span in alternatives:
                start, end = span["start_char"], span["end_char"]
                if (
                    not isinstance(start, int) or not isinstance(end, int)
                    or start < 0 or end <= start
                ):
                    raise ValueError("非法金标区间")
                intervals = []
                for source in candidate["sources"]:
                    if (source["original_block_id"] == span["block_id"]
                            and source["content_sha256"] == span["content_sha256"]):
                        left = max(start, source["start_char"])
                        right = min(end, source["end_char"])
                        if right > left:
                            intervals.append((left, right))
                covered = 0
                last = start
                for left, right in sorted(intervals):
                    covered += max(0, right - max(last, left))
                    last = max(last, right)
                if covered / (end - start) >= overlap_threshold:
                    matched.add(group_no)
        if matched:
            relevant_candidates += 1
            first_rank = first_rank or rank
        found.update(matched)
    complete = gold.get("scope_complete") is True
    grades = gold.get("corpus_grades")
    ndcg = None
    if (complete and gold.get("corpus_grades_complete") is True and isinstance(grades, dict)
            and all(c["chunk_id"] in grades for c in candidates)):
        if any(type(grade) is not int or grade not in (0, 1, 2) for grade in grades.values()):
            raise ValueError("相关度仅支持 0/1/2")
        ideal = sum((2 ** grade - 1) / log2(rank + 2)
                    for rank, grade in enumerate(sorted(grades.values(), reverse=True)[:k]))
        actual = sum((2 ** grades[c["chunk_id"]] - 1) / log2(rank + 2)
                     for rank, c in enumerate(candidates))
        ndcg = actual / ideal if ideal else None
    return {
        "query_id": gold["query_id"], "k": k, "gold_complete": complete,
        "query_family": gold.get("query_family", gold["query_id"]),
        "recall": len(found) / len(groups) if groups and complete else None,
        "hit": int(bool(found)) if groups and complete else None,
        "mrr": (1 / first_rank if first_rank else 0.0) if groups and complete else None,
        "precision": relevant_candidates / k if groups and complete else None,
        "all_evidence_found": int(len(found) == len(groups)) if groups and complete else None,
        "matched_groups": len(found), "total_groups": len(groups),
        "ndcg": ndcg,
    }


def summarize_metrics(
    rows: list[dict[str, Any]], seed: int = 20261111,
    keys: tuple[str, ...] = ("recall", "hit", "mrr", "precision", "all_evidence_found", "ndcg"),
) -> dict[str, Any]:
    """按问题族聚类 bootstrap，簇数不足时保留区间未知。"""
    summary = {}
    for key in keys:
        known = [row for row in rows if row.get(key) is not None]
        groups: dict[str, list[float]] = {}
        for row in known:
            groups.setdefault(str(row.get("query_family", row["query_id"])), []).append(row[key])
        mean = sum(row[key] for row in known) / len(known) if known else None
        interval = None
        if len(groups) >= 2:
            generator = random.Random(seed)
            clusters = list(groups.values())
            estimates = []
            for _ in range(1000):
                sampled = [value for group in generator.choices(clusters, k=len(clusters))
                           for value in group]
                estimates.append(sum(sampled) / len(sampled))
            estimates.sort()
            interval = [estimates[24], estimates[974]]
        summary[key] = {"mean": mean, "n": len(known), "clusters": len(groups),
                        "ci95": interval, "bootstrap_seed": seed}
    return summary
