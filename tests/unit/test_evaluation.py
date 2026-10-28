"""评估支持规则合并拆分，缺少人工相关性标注时不伪造召回率。"""

import pytest

from tc_rag.application.services.evaluation import evaluate


def test_grouped_matching_and_retrieval_recall():
    output = {"rules": [{"rule_id": "r"}]}
    gold = {
        "rules": [{"rule_id": "g1"}, {"rule_id": "g2"}],
        "rule_groups": [{"generated_ids": ["r"], "gold_ids": ["g1", "g2"]}],
        "relevant": {"q": ["e1", "e2"]},
    }
    report = evaluate(
        output, {"bundles": [{"query_id": "q", "hits": [{"object_id": "e1"}]}]}, gold, 1
    )
    assert report["rule_precision"] == report["rule_recall"] == 1
    assert report["evidence_recall_at_k"] == 0.5
    assert report["automatic_point_link_ratio"] is None
    gold["rule_groups"].append(gold["rule_groups"][0])
    with pytest.raises(ValueError):
        evaluate(output, {}, gold, 1)


def test_unknown_relevance_is_not_zero():
    result = evaluate({"rules": []}, {}, {"rules": []}, 3)
    assert result["evidence_recall_at_k"] is None
    assert result["rule_precision"] is None


def test_manual_business_review_is_separate_from_automatic_links():
    payload = {
        "rules": [{"rule_id": "r"}],
        "points": [{"point_id": "p"}],
        "cases": [{"case_id": "c"}],
        "coverage": [{"rule_id": "r", "point_id": "p"}],
    }
    gold = {
        "rules": [{"rule_id": "g"}],
        "manual": {
            "covered_gold_rule_ids": [],
            "covered_point_ids": [],
            "unsupported_case_ids": ["c"],
            "duplicate_case_ids": [],
        },
    }
    result = evaluate(payload, {}, gold, 5)
    assert result["automatic_rule_link_ratio"] == 1
    assert result["manual_gold_rule_coverage"] == result["manual_point_coverage"] == 0
    assert result["manual_unsupported_expectation_ratio"] == 1
    assert result["manual_duplicate_case_ratio"] == 0
