import hashlib
import json
import runpy

import pytest

stable_ties = runpy.run_path("eval/kb/audit_rerank_ties.py")["stable_ties"]


def test_exact_tie_uses_original_rank_without_collapsing_near_scores():
    row = {"before": ["a", "b", "c", "d"], "after": ["d", "c", "b", "a"],
           "scores": {"a": 0.9, "b": 1.0, "c": 1.0, "d": 1.0000001}}
    assert stable_ties(row) == ["d", "b", "c", "a"]
    assert row["after"] == ["d", "c", "b", "a"]


@pytest.mark.parametrize("change", [
    {"after": ["a", "a"]}, {"scores": {"a": 1.0}},
    {"scores": {"a": float("nan"), "b": 1.0}},
    {"scores": {"a": 0.1, "b": 1.0}},
])
def test_rejects_invalid_mapping_nonfinite_or_unsorted(change):
    row = {"before": ["a", "b"], "after": ["a", "b"],
           "scores": {"a": 1.0, "b": 1.0}, **change}
    with pytest.raises(ValueError):
        stable_ties(row)


def test_audit_preserves_capture_and_declares_counterfactual(tmp_path):
    audit = runpy.run_path("eval/kb/audit_rerank_ties.py")["audit"]
    pool = {"questions": [{"query_id": "q", "split": "dev", "candidates": [
        {"chunk_id": "a"}, {"chunk_id": "b"}]}]}
    pool_path, results = tmp_path / "pool.json", tmp_path / "results.jsonl"
    pool_path.write_text(json.dumps(pool), "utf-8")
    row = {"query_id": "q", "status": "success", "before": ["a", "b"],
           "after": ["b", "a"], "scores": {"a": 1.0, "b": 1.0},
           "pool_sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest()}
    original = json.dumps(row) + "\n"
    results.write_text(original, "utf-8")
    audit(pool_path, results, tmp_path / "out")
    derived = json.loads((tmp_path / "out/counterfactual_results.jsonl").read_text("utf-8"))
    assert derived["after"] == ["a", "b"]
    assert derived["experiment_kind"] == "offline_exact_tie_counterfactual"
    assert results.read_text("utf-8") == original
    pool["questions"][0]["split"] = "test"
    pool_path.write_text(json.dumps(pool), "utf-8")
    with pytest.raises(ValueError, match="开发集"):
        audit(pool_path, results, tmp_path / "blocked")
