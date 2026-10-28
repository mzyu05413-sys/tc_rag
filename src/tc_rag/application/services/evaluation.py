"""评估只统计人工给出的关联，不把文本相似度当成语义正确率。"""

from typing import Any


def evaluate(
    payload: dict[str, Any], retrieval: dict[str, Any], gold: dict[str, Any], k: int
) -> dict[str, Any]:
    if k <= 0:
        raise ValueError("k 必须为正")
    predicted = {r["rule_id"] for r in payload["rules"]}
    expected = {r["rule_id"] for r in gold["rules"]}
    matched_pred: set[str] = set()
    matched_gold: set[str] = set()
    for group in gold.get("rule_groups", []):
        left, right = set(group["generated_ids"]), set(group["gold_ids"])
        if not left or not right or not left <= predicted or not right <= expected:
            raise ValueError("人工匹配组引用不存在的规则或空关联")
        if left & matched_pred or right & matched_gold:
            raise ValueError("同一规则不能在多个匹配组中重复计数")
        matched_pred |= left
        matched_gold |= right
    relevant = gold.get("relevant", {})
    recalls = []
    found = {b["query_id"]: b for b in retrieval.get("bundles", [])}
    for query_id, labels in relevant.items():
        if not labels:
            raise ValueError("相关性标注不能为空，不能把未知标为零召回")
        hits = found.get(query_id, {}).get("hits", [])[:k]
        recalls.append(len(set(labels) & {h["object_id"] for h in hits}) / len(set(labels)))
    issues = payload.get("issues", [])
    cases = payload.get("cases", [])
    links = payload.get("coverage", [])
    points = {p["point_id"] for p in payload.get("points", [])}
    unsupported = {
        obj for i in issues if i["kind"] == "unsupported_expectation" for obj in i["object_ids"]
    }
    return {
        "rule_precision": len(matched_pred) / len(predicted)
        if predicted and "rule_groups" in gold
        else None,
        "rule_recall": len(matched_gold) / len(expected)
        if expected and "rule_groups" in gold
        else None,
        "evidence_recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "k": k,
        "judged_queries": len(recalls),
        "automatic_rule_link_ratio": len({link["rule_id"] for link in links} & predicted)
        / len(predicted)
        if predicted
        else None,
        "automatic_point_link_ratio": len({link["point_id"] for link in links} & points)
        / len(points)
        if points
        else None,
        "flagged_expectation_ratio": sum(c["case_id"] in unsupported for c in cases) / len(cases)
        if cases
        else None,
        "gap_count": sum(i["kind"] == "gap" for i in issues),
        "conflict_count": sum(i["kind"] == "conflict" for i in issues),
        "duplicate_issue_count": sum(i["kind"] == "duplicate" for i in issues),
        **manual_metrics(payload, gold),
        "note": "人工匹配组允许一对多/多对一；自动关联率不是人工业务覆盖率，null表示未评估",
    }


def manual_metrics(payload: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    """人工结论单独计量；缺标注时是未知，不能用启发式警告冒充人工比例。"""
    manual = gold.get("manual", {})
    rules = {r["rule_id"] for r in gold["rules"]}
    points = {p["point_id"] for p in payload.get("points", [])}
    cases = {c["case_id"] for c in payload.get("cases", [])}
    result: dict[str, Any] = {}
    for name, field, universe in (
        ("manual_gold_rule_coverage", "covered_gold_rule_ids", rules),
        ("manual_point_coverage", "covered_point_ids", points),
        ("manual_unsupported_expectation_ratio", "unsupported_case_ids", cases),
        ("manual_duplicate_case_ratio", "duplicate_case_ids", cases),
    ):
        labels = set(manual.get(field, []))
        if not labels <= universe:
            raise ValueError("人工质量标注含未知对象")
        result[name] = len(labels) / len(universe) if field in manual and universe else None
    return result
