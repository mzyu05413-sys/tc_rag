"""知识库指标去重，待审核和不完整标注保留未知。"""

import pytest

from tc_rag.application.kb.evaluate import score_retrieval, summarize_metrics


def material():
    span = {"block_id": "b", "content_sha256": "h", "start_char": 0, "end_char": 10}
    source = {"original_block_id": "b", "content_sha256": "h",
              "start_char": 0, "end_char": 6}
    gold = {"query_id": "q", "review_status": "reviewed", "scope_complete": True,
            "evidence_groups": [[span]]}
    result = {"top_k": 3, "candidates": [{"sources": [source, source]}]}
    return result, gold


def test_retrieval_counts_groups_and_fixed_k():
    result, gold = material()
    metrics = score_retrieval(result, gold)
    assert metrics["recall"] == 1
    assert metrics["precision"] == 1 / 3
    assert score_retrieval(result, gold, overlap_threshold=0.8)["recall"] == 0


def test_unknown_and_pending_labels():
    result, gold = material()
    gold["scope_complete"] = False
    assert score_retrieval(result, gold)["recall"] is None
    gold["review_status"] = "pending"
    with pytest.raises(ValueError, match="reviewed"):
        score_retrieval(result, gold)


def test_ndcg_requires_complete_corpus_grades_and_bootstrap_reproducible():
    result, gold = material()
    result["candidates"][0]["chunk_id"] = "c1"
    gold["corpus_grades"] = {"c1": 2, "c2": 1}
    assert score_retrieval(result, gold)["ndcg"] is None
    gold["corpus_grades_complete"] = True
    assert 0 < score_retrieval(result, gold)["ndcg"] < 1
    rows = [{"query_id": "q1", "query_family": "a", "recall": 0.0},
            {"query_id": "q2", "query_family": "b", "recall": 1.0}]
    assert summarize_metrics(rows) == summarize_metrics(rows)
    assert summarize_metrics(rows)["recall"]["ci95"] == [0.0, 1.0]
