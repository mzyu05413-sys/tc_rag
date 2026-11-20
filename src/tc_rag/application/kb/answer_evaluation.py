"""人工审核结果的确定性汇总，不自动判定业务语义。"""

from typing import Any


def score_answer(
    result: dict[str, Any], gold: dict[str, Any], review: dict[str, Any],
) -> dict[str, Any]:
    if gold.get("review_status") != "reviewed" or review.get("review_status") != "reviewed":
        raise ValueError("金标和回答审核必须人工确认")
    if result.get("status") != "completed":
        raise ValueError("服务失败不得作为回答或拒答评分")
    if review.get("run_id") != result["run_id"]:
        raise ValueError("人工审核与回答运行不匹配")
    expected_refusal = gold.get("should_abstain")
    if type(expected_refusal) is not bool:
        raise ValueError("金标须明确 should_abstain")
    claims = result["answer"]["claims"]
    citation_count = sum(len(claim["citations"]) for claim in claims)
    units = gold.get("answer_units")
    if not isinstance(units, list):
        raise ValueError("答案要点未知")
    fields = {"claim_supported": len(claims), "citation_supported": citation_count,
              "claim_citation_supported": len(claims), "answer_units_covered": len(units)}
    for field, count in fields.items():
        values = review.get(field)
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(f"{field} 审核数量不匹配")
        if any(type(value) is not bool for value in values):
            raise ValueError("人工判定必须是布尔值，未知不能当负例")
    if type(review.get("correct")) is not bool:
        raise ValueError("须人工判定总体正确性")
    refused = result["answer"]["status"] == "abstained"
    return {
        "query_id": gold["query_id"], "correctness": int(review["correct"]),
        "query_family": gold.get("query_family", gold["query_id"]),
        "faithfulness": sum(review["claim_supported"]) / len(claims) if claims else None,
        "citation_precision": sum(review["citation_supported"]) / citation_count
        if citation_count else None,
        "citation_recall": sum(review["claim_citation_supported"]) / len(claims)
        if claims else None,
        "completeness": sum(review["answer_units_covered"]) / len(units) if units else None,
        "refusal_tp": int(refused and expected_refusal),
        "refusal_fp": int(refused and not expected_refusal),
        "refusal_fn": int(not refused and expected_refusal),
        "should_abstain": expected_refusal,
    }
