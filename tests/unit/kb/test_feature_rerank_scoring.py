import hashlib
import json
import runpy

import pytest

score = runpy.run_path("eval/kb/score_feature_rerank.py")["score"]


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), "utf-8")


def material(tmp_path):
    pool, gold, runs = tmp_path / "pool.json", tmp_path / "gold", tmp_path / "runs"
    ids = ["c", "b", "d", "a"]
    put(pool, dict(snapshot_id="s", questions=[dict(query_id="q", split="dev",
        candidates=[dict(chunk_id=cid, text=cid, sources=[dict(
            block_id=cid, start_char=0, end_char=1)]) for cid in ids])]))
    digest = hashlib.sha256(pool.read_bytes()).hexdigest()
    put(gold / "scope_manifest.json", dict(snapshot_id="s", gold_complete=True,
                                          question_scopes=dict(q=ids)))
    put(gold / "labels.reviewed.json", [dict(query_id="q", chunk_id=cid,
        snapshot_id="s", label=int(cid == "a"), review_status="reviewed") for cid in ids])
    put(gold / "tasks.reviewed.json", [dict(task_id="q", rules=[dict(rule_id="r",
        evidence=[dict(block_id="a", start_char=0, end_char=1)])])])
    plan = tmp_path / "plan.json"
    put(plan, dict(source_hashes={}, expected_model="rerank", tie_policy="input_order",
        jobs=[dict(job_id="j", pool_path=str(pool), pool_sha256=digest,
                   gold_directory=str(gold), batch="round1", mode="vector",
                   evidence_policy="body_only")]))
    put(runs / "execution.json", dict(plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest()))
    put(runs / "j/complete.json", dict(pool_sha256=digest, model="rerank",
        tie_policy="input_order", input_policy="original_frozen_chunk_text"))
    result = dict(query_id="q", status="success", before=ids, after=["a", "b", "c", "d"],
                  scores=dict(a=1.0, b=0.8, c=0.5, d=0.3), pool_sha256=digest,
                  tie_policy="input_order")
    (runs / "j/results.jsonl").write_text(json.dumps(result)+"\n", "utf-8")
    return plan, runs, result


def test_actual_pair_scores_and_rules_without_changing_ranking(tmp_path):
    plan, runs, _ = material(tmp_path)
    raw = (runs / "j/results.jsonl").read_bytes()
    out = tmp_path / "output"
    score(plan, runs, out)
    result = json.loads((out / "comparison.json").read_text("utf-8"))
    group = result["groups"][0]
    before = next(r for r in group["summary"] if r["mode"] == "before" and r["k"] == 3)
    after = next(r for r in group["summary"] if r["mode"] == "after" and r["k"] == 3)
    assert before["counts"]["tp"] == 0 and before["complete_rules"] == 0
    assert after["counts"]["tp"] == 1 and after["complete_rules"] == 1
    assert group["paired_n"] == 1 and group["evidence_policy"] == "body_only"
    assert (runs / "j/results.jsonl").read_bytes() == raw
    with pytest.raises(ValueError, match="已存在"):
        score(plan, runs, out)


def test_service_error_is_excluded_not_scored_zero(tmp_path):
    plan, runs, row = material(tmp_path)
    row = {key: row[key] for key in ("query_id", "pool_sha256", "tie_policy")}
    row.update(status="error", error_type="TimeoutError")
    (runs / "j/results.jsonl").write_text(json.dumps(row)+"\n", "utf-8")
    out = tmp_path / "output"
    score(plan, runs, out)
    result = json.loads((out / "comparison.json").read_text("utf-8"))["groups"][0]
    assert result["paired_n"] == 0 and len(result["errors"]) == 1
    assert all(r["macro"]["recall"] is None and r["macro_rule_coverage"] is None
               for r in result["summary"])


def test_tie_policy_violation_rejected_before_scoring(tmp_path):
    plan, runs, row = material(tmp_path)
    row["scores"] = {cid: 1.0 for cid in row["before"]}
    (runs / "j/results.jsonl").write_text(json.dumps(row)+"\n", "utf-8")
    with pytest.raises(ValueError, match="同分保序"):
        score(plan, runs, tmp_path / "output")


def test_model_configuration_change_rejected(tmp_path):
    plan, runs, _ = material(tmp_path)
    path = runs / "j/complete.json"
    record = json.loads(path.read_text("utf-8"))
    record["model"] = "different"
    put(path, record)
    with pytest.raises(ValueError, match="模型"):
        score(plan, runs, tmp_path / "output")
