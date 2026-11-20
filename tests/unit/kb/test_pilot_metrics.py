import pytest

from tc_rag.application.kb.pilot_metrics import score_pilot, summarize_pilot


def score(c, u, g, k=5):
    spans = [{"block_id": str(i), "content_sha256": "h", "start_char": 0,
              "end_char": 2} for i in range(g)]
    candidates = [{"chunk_id": str(i), "sources": [
        dict(spans[j], original_block_id=str(j)) for j in range(u)
    ]} for i in range(c)]
    return score_pilot({"top_k": k, "candidates": candidates}, {
        "query_id": "q", "review_status": "reviewed", "scope_complete": True,
        "evidence_groups": [[s] for s in spans],
    })


def test_hand_calculation():
    summary = summarize_pilot([score(1, 1, 1), score(2, 2, 4), score(0, 0, 2)])
    assert "f1" not in summary["macro"]
    assert summary["micro"]["recall"] == pytest.approx(3/7)
    assert "f1" not in summary["micro"]
    assert summary["macro"]["precision"] == pytest.approx(summary["micro"]["precision"])


def test_repeated_evidence_and_multiple_groups():
    row = score(5, 1, 4)
    assert row["covered_groups"] == 1
    assert row["precision"] == 1
    assert row["recall"] == .25
    assert "f1" not in row
    assert score(1, 3, 3)["recall"] == 1


def test_missing_slots_and_no_answer():
    assert score(1, 1, 1)["missing_slots"] == 4
    row = score(0, 0, 0)
    assert row["recall"] is None
    assert "f1" not in row
    assert summarize_pilot([row])["n"] == 0


def test_duplicate_candidates_rejected():
    with pytest.raises(ValueError, match="重复"):
        score_pilot({"top_k": 2, "candidates": [
            {"chunk_id": "x"}, {"chunk_id": "x"}]}, {})


@pytest.mark.parametrize("complete", [None, False])
def test_incomplete_scope_excluded(complete):
    gold = {"query_id": "q", "review_status": "reviewed", "scope_complete": complete,
            "evidence_groups": [[{"block_id": "中文块", "content_sha256": "h",
                                  "start_char": 1, "end_char": 5}]]}
    result = {"top_k": 5, "candidates": [{"chunk_id": "a", "sources": [
        {"original_block_id": "中文块", "content_sha256": "h",
         "start_char": 1, "end_char": 3}]}]}
    row = score_pilot(result, gold)
    assert row["covered_groups"] == 1
    assert row["recall"] is None
    assert "f1" not in row
    assert summarize_pilot([row])["excluded"] == 1


def test_alternative_span_and_exact_threshold():
    span = {"block_id": "中文块", "content_sha256": "h", "start_char": 1, "end_char": 5}
    gold = {"query_id": "q", "review_status": "reviewed", "scope_complete": True,
            "evidence_groups": [[dict(span, block_id="other"), span]]}
    source = dict(span, original_block_id="中文块", end_char=3)
    result = {"top_k": 5, "candidates": [{"chunk_id": "a", "sources": [source, source]}]}
    assert score_pilot(result, gold)["recall"] == 1
    source["end_char"] = 2
    assert score_pilot(result, gold)["recall"] == 0
