"""人工回答评分按实际声明、引用及要点数量核对。"""

import pytest

from tc_rag.application.kb.answer_evaluation import score_answer


def test_manual_refusal_and_unknown_rejection():
    result = {"run_id": "a", "status": "completed",
              "answer": {"status": "abstained", "claims": [], "reason": "无依据"}}
    gold = {"query_id": "q", "review_status": "reviewed", "should_abstain": True,
            "answer_units": []}
    review = {"run_id": "a", "review_status": "reviewed", "correct": True,
              "claim_supported": [], "citation_supported": [],
              "claim_citation_supported": [], "answer_units_covered": []}
    row = score_answer(result, gold, review)
    assert row["refusal_tp"] == 1
    assert row["faithfulness"] is None
    review["correct"] = None
    with pytest.raises(ValueError):
        score_answer(result, gold, review)
