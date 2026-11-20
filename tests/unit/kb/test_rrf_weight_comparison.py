import hashlib
import json
import runpy

import pytest

module = runpy.run_path("eval/kb/compare_rrf_weights.py")
fuse, compare = module["fuse"], module["compare"]


def test_integer_weight_reuses_rrf_with_expected_score():
    result = dict(fuse(["a", "b"], ["b", "c"], 2))
    assert result["a"] == pytest.approx(2/61)
    assert result["b"] == pytest.approx(2/62 + 1/61)
    assert result["c"] == pytest.approx(1/62)
    assert len(result) == 3


@pytest.mark.parametrize("weight", [0, -1, True, 3])
def test_unplanned_weight_rejected(weight):
    with pytest.raises(ValueError, match="权重"):
        fuse(["a"], ["b"], weight)


def put(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), "utf-8")


def material(tmp_path):
    run, gold, snapshot = tmp_path / "run", tmp_path / "gold", tmp_path / "snapshot.json"
    chunks = [dict(chunk_id=cid, text=cid, sources=[dict(
        block_id=cid, start_char=0, end_char=1)]) for cid in ("a", "b")]
    candidates = [dict(query_id="q", mode=mode, snapshot_id="s", candidates=chunks,
                       vector_ranking=["a", "b"], keyword_ranking=["b", "a"])
                  for mode in ("vector", "keyword", "hybrid")]
    put(run / "candidates.json", candidates)
    put(run / "complete.json", dict(budget=40, rrf_constant=60, rerank=False,
                                     keyword_algorithm="bm25"))
    put(run / "scores.json", dict(snapshot_id="s", rows=[dict(
        query_id="q", mode="hybrid", k=k, tp=1, fp=1, fn=0, tn=0,
        complete_rule_ids=["r"]) for k in (3, 5, 10)]))
    put(gold / "tasks.reviewed.json", [dict(task_id="q", split="dev", rules=[dict(
        rule_id="r", evidence=[dict(block_id="a", start_char=0, end_char=1)])])])
    put(gold / "scope_manifest.json", dict(snapshot_id="s", gold_complete=True,
                                          question_scopes=dict(q=["a", "b"])))
    put(gold / "labels.reviewed.json", [dict(query_id="q", chunk_id=cid, snapshot_id="s",
        label=int(cid == "a"), body_label=int(cid == "a"), review_status="reviewed",
        text_sha256=hashlib.sha256(cid.encode()).hexdigest()) for cid in ("a", "b")])
    put(snapshot, dict(snapshot=dict(snapshot_id="s"), chunks=chunks))
    return run, gold, snapshot


def test_comparison_reproduces_baseline_and_leaves_inputs_unchanged(tmp_path):
    args = material(tmp_path)
    original = (args[0] / "candidates.json").read_bytes()
    compare(*args, tmp_path / "output")
    payload = json.loads((tmp_path / "output/comparison.json").read_text("utf-8"))
    assert payload["baseline_exact_reproduction"] is True and payload["model_calls"] == 0
    assert len(payload["rows"]) == 6 and len(payload["summary"]) == 6
    assert (args[0] / "candidates.json").read_bytes() == original
    assert all(r["complete_rules"] == 1 for r in payload["summary"])
    with pytest.raises(ValueError, match="已存在"):
        compare(*args, tmp_path / "output")


def test_changed_baseline_refuses_pairing(tmp_path):
    args = material(tmp_path)
    path = args[0] / "candidates.json"
    rows = json.loads(path.read_text("utf-8"))
    rows[2]["candidates"].reverse()
    put(path, rows)
    with pytest.raises(ValueError, match="复现基线"):
        compare(*args, tmp_path / "output")
    assert not (tmp_path / "output").exists()
