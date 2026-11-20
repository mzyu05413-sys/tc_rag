"""候选精确率与事实覆盖率分开汇总，不计算跨粒度F1。"""

from typing import Any

from tc_rag.application.kb.evaluate import score_retrieval


def score_pilot(result: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    """相关候选 C 与去重证据 U 分别统计。服务错误由运行器另列。"""
    candidates = result["candidates"][:result["top_k"]]
    ids = [c["chunk_id"] for c in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("重复候选 ID")
    row = score_retrieval(result, gold)
    k = row["k"]
    # 基础评分已完成来源区间匹配；precision 未知时不反推计数。
    complete_gold = dict(gold, scope_complete=True)
    counts = score_retrieval(result, complete_gold)
    c = round(counts["precision"] * k) if counts["precision"] is not None else 0
    row.update(
        relevant_candidates=c, covered_groups=row["matched_groups"], requested_k=k,
        returned_count=len(candidates), irrelevant_returned_count=len(candidates) - c,
        missing_slots=k - len(candidates),
    )
    return row


def summarize_pilot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只对同一可评分集合计算宏微平均，同时保留排除数。"""
    valid = [r for r in rows if r["precision"] is not None and r["recall"] is not None]
    if not valid:
        return {"n": 0, "excluded": len(rows), "macro": None, "micro": None}
    p = sum(r["relevant_candidates"] for r in valid) / sum(r["requested_k"] for r in valid)
    r = sum(r["covered_groups"] for r in valid) / sum(r["total_groups"] for r in valid)
    return {
        "n": len(valid), "excluded": len(rows) - len(valid),
        "macro": {key: sum(v[key] for v in valid) / len(valid)
                  for key in ("precision", "recall")},
        "micro": {"precision": p, "recall": r},
    }
