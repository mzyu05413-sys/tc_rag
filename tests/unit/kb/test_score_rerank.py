import hashlib
import json
import runpy

import pytest


def test_same_pool_validation():
    validate = runpy.run_path("eval/kb/score_rerank.py")["validate_pair"]
    pool = {"candidates": [{"chunk_id": "a"}, {"chunk_id": "b"}]}
    validate(pool, {"before": ["a", "b"], "after": ["b", "a"]})
    for row in [{"before": ["b", "a"], "after": ["b", "a"]},
                {"before": ["a", "b"], "after": ["a", "a"]}]:
        with pytest.raises(ValueError):
            validate(pool, row)


def scoring_inputs(tmp_path, labels=(1, 0, 1)):
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"snapshot_id": "s", "questions": [{
        "query_id": "q", "split": "dev", "candidates": [
            {"chunk_id": "a"}, {"chunk_id": "b"}]}]}), "utf-8")
    result = tmp_path / "results.jsonl"
    result.write_text(json.dumps({"query_id": "q", "status": "success", "before": ["a", "b"],
        "after": ["b", "a"], "pool_sha256": hashlib.sha256(pool.read_bytes()).hexdigest()}),
        "utf-8")
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"query_id": "q", "chunk_id": key, "snapshot_id": "s",
        "label": value, "review_status": "reviewed"} for key, value in zip("abc", labels)]),
        "utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"snapshot_id": "s", "question_scopes": {"q": list("abc")}}),
                     "utf-8")
    return pool, result, gold, scope


def test_explicit_scope_precision_uses_actual_return_count_and_micro(tmp_path):
    score = runpy.run_path("eval/kb/score_rerank.py")["score"]
    pool, result, gold, scope = scoring_inputs(tmp_path)
    score(pool, result, gold, tmp_path / "out", scope_manifest=scope)
    report = json.loads((tmp_path / "out/scores.json").read_text("utf-8"))
    row = next(r for r in report["summary"] if r["mode"] == "after" and r["k"] == 5)
    assert row["counts"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 0}
    assert row["macro"]["precision"] == .5 and row["micro"]["recall"] == .5
    assert report["universe_sizes"] == {"q": 3}


@pytest.mark.parametrize("bad", ["unknown", "missing", "snapshot", "no_protocol"])
def test_incomplete_or_unknown_gold_cannot_be_scored(tmp_path, bad):
    score = runpy.run_path("eval/kb/score_rerank.py")["score"]
    pool, result, gold, scope = scoring_inputs(tmp_path)
    labels = json.loads(gold.read_text("utf-8"))
    if bad == "unknown":
        labels[0]["label"] = None
    elif bad == "missing":
        labels.pop()
    elif bad == "snapshot":
        labels[0]["snapshot_id"] = "other"
    gold.write_text(json.dumps(labels), "utf-8")
    with pytest.raises(ValueError):
        score(pool, result, gold, tmp_path / "out",
              scope_manifest=None if bad == "no_protocol" else scope)
    assert not (tmp_path / "out").exists()


def test_zero_positive_recall_stays_null(tmp_path):
    score = runpy.run_path("eval/kb/score_rerank.py")["score"]
    pool, result, gold, scope = scoring_inputs(tmp_path, labels=(0, 0, 0))
    score(pool, result, gold, tmp_path / "out", scope_manifest=scope)
    report = json.loads((tmp_path / "out/scores.json").read_text("utf-8"))
    row = next(r for r in report["summary"] if r["mode"] == "after" and r["k"] == 5)
    assert row["macro"]["recall"] is None and row["valid_counts"]["recall"] == 0
    assert row["macro"]["rr"] is None and row["valid_counts"]["rr"] == 0
