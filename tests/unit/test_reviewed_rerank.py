"""候选人工标注导入与严格配对评分，不联网、不读取真实需求材料。"""

import copy

import pytest
from reviewed_rerank import evaluate_history, normalize_review, strict_metrics, validate_review


@pytest.fixture
def material():
    query = {
        "record_id": "Q001", "query_id": "hash", "text": "查询", "stage": "plan_points",
        "direct_ids": [], "notes": "原始说明", "relevant_groups": ["G001"],
    }
    seed = {
        "schema_version": "1.0", "source_run_id": "run_fixture", "dataset_id": "dataset",
        "seed_revision": "revision", "queries": [query],
        "groups": [
            {"group_id": "G001", "corpus_ids": ["C001", "C002"]},
            {"group_id": "G002", "corpus_ids": ["C003"]},
            {"group_id": "G003", "corpus_ids": ["C004"]},
        ],
    }
    review = {
        **{key: seed[key] for key in (
            "schema_version", "source_run_id", "dataset_id", "seed_revision"
        )},
        "tool": "tc-rag-annotation",
        "progress": {"candidate_confirmed": 1, "corpus_complete": 0, "total": 1},
        "queries": [{
            **query, "notes": "用户说明", "original_notes": "原始说明",
            "judgments": {"G001": "relevant", "G002": "unsure"},
            "confirmed": True, "confirmed_at": None, "reviewed_by": "user-via-web",
            "review_scope": "presented_candidates", "corpus_review_complete": False,
            "relevant_corpus_ids": ["C001", "C002"], "irrelevant_corpus_ids": [],
            "irrelevant_groups": [], "uncertain_groups": ["G002"],
            "unreviewed_groups": ["G003"],
        }],
    }
    return review, seed


def test_validation_accepts_partial_candidate_review(material):
    review, seed = material
    validate_review(review, seed)


@pytest.mark.parametrize("damage", [
    "dataset", "qa", "duplicate", "query", "unknown", "decision", "expanded", "progress",
    "complete", "unknown_list", "original_notes", "unconfirmed_positive",
])
def test_validation_rejects_inconsistent_or_fake_reviews(material, damage):
    review, seed = material
    entry = review["queries"][0]
    if damage == "dataset":
        review["dataset_id"] = "another"
    elif damage == "qa":
        review["qa_simulation"] = True
    elif damage == "duplicate":
        review["queries"].append(copy.deepcopy(entry))
    elif damage == "query":
        entry["text"] = "另一个查询"
    elif damage == "unknown":
        entry["judgments"]["G999"] = "relevant"
    elif damage == "decision":
        entry["judgments"]["G001"] = "maybe"
    elif damage == "expanded":
        entry["relevant_corpus_ids"] = ["C001"]
    elif damage == "progress":
        review["progress"]["candidate_confirmed"] = 0
    elif damage == "complete":
        entry["corpus_review_complete"] = True
    elif damage == "unknown_list":
        entry["unreviewed_groups"] = []
    elif damage == "original_notes":
        entry["original_notes"] = "改写原始说明"
    elif damage == "unconfirmed_positive":
        entry["confirmed"] = False
    with pytest.raises(ValueError):
        validate_review(review, seed)


def test_chat_correction_is_auditable_and_does_not_change_source(material):
    review, seed = material
    before = copy.deepcopy(review)
    correction = {
        "record_id": "Q001", "group_id": "G002", "expected": "unsure", "value": "relevant",
        "note": "用户明确改为相关", "source": "user-via-chat",
    }
    grouped, expanded = normalize_review(review, seed, [correction])
    assert review == before
    assert grouped[0]["relevant_groups"] == ["G001", "G002"]
    assert grouped[0]["uncertain_groups"] == []
    assert grouped[0]["unreviewed_groups"] == ["G003"]
    assert grouped[0]["chat_corrections"] == [correction]
    assert not grouped[0]["corpus_review_complete"]
    assert expanded[0]["relevant_corpus_ids"] == ["C001", "C002", "C003"]
    assert expanded[0]["unreviewed_corpus_ids"] == ["C004"]


def test_correction_requires_expected_old_value(material):
    review, seed = material
    with pytest.raises(ValueError, match="旧值"):
        normalize_review(review, seed, [{
            "record_id": "Q001", "group_id": "G002", "expected": "irrelevant",
            "value": "relevant", "note": "补充", "source": "user-via-chat",
        }])


def test_unknown_at_rank_one_blocks_mrr_but_not_known_hit():
    result = strict_metrics(["unknown", "p1", "p2", "p3", "p4"],
                            {"p1", "p2", "p3", "p4", "p5"}, set())
    assert result == {"recall@5": None, "hit@5": 1.0, "mrr@10": None, "ndcg@5": None}


def test_unknown_after_first_positive_does_not_change_mrr():
    result = strict_metrics(["n", "p", "unknown"], {"p"}, {"n"})
    assert result["mrr@10"] == 0.5
    assert result["ndcg@5"] is None
    assert result["recall@5"] is None


def test_idcg_is_known_with_at_least_five_confirmed_positives():
    result = strict_metrics(["p1", "p2", "p3", "p4", "p5"],
                            {"p1", "p2", "p3", "p4", "p5"}, set())
    assert result["ndcg@5"] == 1.0
    assert result["recall@5"] is None


def test_idcg_is_unknown_with_fewer_positives_until_full_corpus_review():
    partial = strict_metrics(["p", "n"], {"p"}, {"n"})
    full = strict_metrics(["p", "n"], {"p"}, {"n"}, complete=True)
    assert partial["ndcg@5"] is None
    assert full["ndcg@5"] == full["recall@5"] == 1.0


def test_known_negatives_allow_mrr_zero_but_unknown_does_not():
    assert strict_metrics(["n"], {"p"}, {"n"})["mrr@10"] == 0.0
    assert strict_metrics(["u"], {"p"}, set())["mrr@10"] is None


def test_paired_averages_use_identical_query_subset_and_keep_unknowns():
    entries, history = [], []
    for number in range(2):
        rid = f"Q{number}"
        entries.append({
            "record_id": rid, "query_id": rid, "stage": "plan_points", "direct_ids": [],
            "confirmed": True, "corpus_review_complete": False,
            "relevant_corpus_ids": ["p"], "irrelevant_corpus_ids": ["n"],
        })
        before, after = ["p", "u", "n"], ["p", "n", "u"]
        if number == 1:
            after = ["u", "p", "n"]
        history.append({
            "record_id": rid, "query_id": rid, "direct_ids": [],
            "vector_hits": [{"object_id": cid, "rank": i} for i, cid in enumerate(before, 1)],
            "hits": [{"object_id": cid, "rank": i} for i, cid in enumerate(after, 1)],
            "rerank": {"status": "succeeded", "scores": {cid: 1 for cid in before}},
            "timings_ms": {"rerank_ms": 10},
        })
    corpus = [{"object_id": cid, "corpus_id": cid} for cid in ("p", "n", "u")]
    result = evaluate_history(entries, corpus, history)
    mrr = result["aggregates"]["all"]["mrr@10"]
    assert mrr["paired_queries"] == ["Q0"]
    assert mrr["excluded_queries"] == ["Q1"]
    assert mrr["before"] == mrr["after"] == 1.0
    assert result["aggregates"]["all"]["recall@5"]["before"] is None
    assert result["new_model_calls"] == 0
    history[1]["hits"].pop()
    with pytest.raises(ValueError, match="候选集"):
        evaluate_history(entries, corpus, history)
