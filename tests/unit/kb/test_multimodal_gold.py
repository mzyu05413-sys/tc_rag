import hashlib
import json
import runpy

import pytest

publish = runpy.run_path("eval/kb/publish_image_gold.py")["publish"]
score = runpy.run_path("eval/kb/score_multimodal_replay.py")["score"]


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")


def material(tmp_path):
    body, audit = tmp_path / "body", tmp_path / "audit"
    image = tmp_path / "image.png"
    image.write_bytes(b"fake-image")
    text = "【图片可见事实】\n- f1 [overview] 编辑按钮。\n【正文关联解释】\n- f2 权限限制。"
    labels = [dict(query_id="q", chunk_id=cid, snapshot_id="s", label=label,
                   review_status="reviewed", text_sha256=hashlib.sha256(
                       value.encode("utf-8")).hexdigest())
              for cid, label, value in [("body", 1, "正文"), ("vision", 0, text)]]
    put(body / "labels.reviewed.json", labels)
    put(body / "scope_manifest.json", dict(snapshot_id="s", gold_complete=True,
                                          question_scopes=dict(q=["body", "vision"])))
    put(body / "tasks.reviewed.json", [])
    put(audit / "evidence.prelabel.json", dict(snapshot_id="s", hashes={}, images=[dict(
        image_key="i", task_id="q", copied_path=str(image),
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        proposed_relevance=None, fact_ids=["f1"], vision_block_id="v",
        vision_text=text, rules=["r"])]))
    decisions, snapshot = tmp_path / "decisions.json", tmp_path / "snapshot.json"
    put(decisions, dict(image_decisions=dict(i=1), prototype_adaptation=dict(
        image_key="other", fact_ids=[], rules=[])))
    put(snapshot, dict(snapshot=dict(snapshot_id="s"),
                       blocks=[dict(block_id="v", origin="vision", content=text)],
                       chunks=[dict(chunk_id="body", text="正文", sources=[]),
                               dict(chunk_id="vision", text=text, sources=[dict(
                                   block_id="v", start_char=0, end_char=len(text))])]))
    return body, audit, decisions, snapshot


def test_publish_preserves_body_and_adds_only_verified_fact(tmp_path):
    args = material(tmp_path)
    publish(*args, tmp_path / "gold")
    rows = json.loads((tmp_path / "gold/labels.reviewed.json").read_text("utf-8"))
    assert [r["label"] for r in rows] == [1, 1]
    assert [r["body_label"] for r in rows] == [1, 0]
    assert rows[1]["image_evidence"][0]["coverage"] == "partial_rule_support"
    assert json.loads((args[0] / "labels.reviewed.json").read_text("utf-8"))[1]["label"] == 0
    with pytest.raises(ValueError, match="已存在"):
        publish(*args, tmp_path / "gold")


def test_unknown_cannot_become_negative(tmp_path):
    args = material(tmp_path)
    put(args[2], dict(image_decisions={}, prototype_adaptation=dict(image_key="other")))
    with pytest.raises(ValueError, match="未知"):
        publish(*args, tmp_path / "gold")


def test_context_restatement_cannot_be_image_fact(tmp_path):
    args = material(tmp_path)
    put(args[2], dict(image_decisions=dict(i=1), prototype_adaptation=dict(
        image_key="i", fact_ids=["f2"], rules=["r"])))
    with pytest.raises(ValueError, match="可见事实"):
        publish(*args, tmp_path / "gold")


def make_run(directory):
    candidates, rows = [], []
    for mode in ("vector", "keyword", "hybrid"):
        candidates.append(dict(query_id="q", mode=mode, snapshot_id="s",
                               candidates=[dict(chunk_id="body"), dict(chunk_id="vision")]))
        for k in (1, 3, 5, 10):
            rows.append(dict(query_id="q", mode=mode, k=k, tp=1, fn=0,
                             fp=int(k > 1), tn=int(k == 1),
                             complete_rule_ids=["r"], rule_count=1))
    put(directory / "candidates.json", candidates)
    put(directory / "scores.json", dict(snapshot_id="s", rows=rows))


def test_same_rankings_replay_and_mismatch_rejection(tmp_path):
    args = material(tmp_path)
    gold, run = tmp_path / "gold", tmp_path / "run"
    publish(*args, gold)
    make_run(run)
    digest = hashlib.sha256((run / "candidates.json").read_bytes()).hexdigest()
    score(run, gold, tmp_path / "scored")
    payload = json.loads((tmp_path / "scored/scores.json").read_text("utf-8"))
    assert len(payload["rows"]) == 12 and payload["model_calls"] == 0
    assert payload["rows"][1]["tp"] == 2
    assert payload["rows"][1]["retained_body_complete_rule_ids"] == ["r"]
    assert hashlib.sha256((run / "candidates.json").read_bytes()).hexdigest() == digest
    original = json.loads((run / "scores.json").read_text("utf-8"))
    original["rows"][0]["tp"] = 0
    put(run / "scores.json", original)
    with pytest.raises(ValueError, match="不能复现"):
        score(run, gold, tmp_path / "mismatched")
