"""同金标与协议上的逐题配对差值，不将不完整样本填成零。"""

from typing import Any

from tc_rag.application.kb.evaluate import summarize_metrics


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    for field in ("gold_semantic_fingerprint", "protocol", "mode"):
        if field not in baseline or baseline[field] != candidate.get(field):
            raise ValueError("比较必须使用相同金标内容、模式与评分协议")
    left = {row["query_id"]: row for row in baseline["rows"]}
    right = {row["query_id"]: row for row in candidate["rows"]}
    if len(left) != len(baseline["rows"]) or len(right) != len(candidate["rows"]):
        raise ValueError("题号重复")
    if left.keys() != right.keys():
        raise ValueError("配对问题集合不一致")
    keys: tuple[str, ...] = (
        "recall", "hit", "mrr", "precision", "all_evidence_found", "ndcg",
    )
    if baseline["mode"] == "qa":
        keys = ("correctness", "faithfulness", "citation_precision", "citation_recall",
                "completeness")
    differences = []
    for query_id, previous in left.items():
        current = right[query_id]
        if (previous.get("k") is not None and current.get("k") is not None
                and previous["k"] != current["k"]):
            raise ValueError("配对检索 k 不一致")
        if previous.get("query_family") != current.get("query_family"):
            raise ValueError("配对统计单元不一致")
        row = {"query_id": query_id, "query_family": previous.get("query_family", query_id)}
        for key in keys:
            a, b = previous.get(key), current.get(key)
            row[key] = b - a if a is not None and b is not None else None
        differences.append(row)
    return {"direction": "candidate_minus_baseline", "pairs": len(differences),
            "metrics": summarize_metrics(differences, keys=keys), "rows": differences}
