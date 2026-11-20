import json
import runpy

import pytest

apply_review = runpy.run_path("eval/kb/apply_feature_review.py")["apply"]


def fixture_files(tmp_path):
    task = {"task_id": "t", "feature": "f", "request": "task request",
        "in_scope": ["in"], "out_of_scope": ["outside"], "document_version": "v",
        "snapshot_id": "s", "rules": [{"rule_id": "r", "statement": "original rule",
            "review_status": "pending", "evidence": [{"block_id": "v:0", "start_char": 0,
                "end_char": 4, "quote": "rule", "anchor_chunk_ids": ["c"]}]}]}
    snapshot = {"snapshot": {"snapshot_id": "s"}, "blocks": [
        {"block_id": "v:0", "content": "rule", "origin": "text", "kind": "paragraph"},
        {"block_id": "v:1", "content": "derived", "origin": "vision", "kind": "other"}],
        "chunks": [{"chunk_id": "c", "text": "rule", "sources": [
            {"block_id": "v:0", "start_char": 0, "end_char": 4}]},
            {"chunk_id": "vision", "text": "derived", "sources": [
                {"block_id": "v:1", "start_char": 0, "end_char": 7}]}]}
    decisions = {"reviewer": "user", "review_date": "2026-11-18", "task_decisions": {
        "t": {"feedback_quote": "accepted", "rule_updates": {"r": "clarified rule"},
            "clarifications": [{"source": "user_review", "required_from_retrieval": False}],
            "excluded": [], "open_questions": []}}, "annotation_decisions": {
        "positive_chunk_orders": {"t": [0]}, "negative_direct_text_chunk_orders": {"t": []}}}
    paths = []
    for name, payload in [("draft", [task]), ("decisions", decisions), ("snapshot", snapshot),
                          ("scope", {"snapshot_id": "s",
                                     "question_scopes": {"t": ["c", "vision"]}})]:
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(payload), "utf-8")
        paths.append(p)
    review = tmp_path / "review.md"
    review.write_text("## f\ntask request\nin\noutside\noriginal rule\naccepted", "utf-8")
    return paths[0], review, paths[1], paths[2], paths[3]


def test_business_adoption_does_not_approve_chunk_labels_or_overwrite_sources(tmp_path):
    paths = fixture_files(tmp_path)
    original = [p.read_bytes() for p in paths]
    apply_review(*paths, tmp_path / "out")
    tasks = json.loads((tmp_path / "out/tasks.reviewed.json").read_text("utf-8"))
    labels = json.loads((tmp_path / "out/labels.prelabel.json").read_text("utf-8"))
    assert tasks[0]["review_status"] == "reviewed"
    assert tasks[0]["rules"][0]["statement"] == "clarified rule"
    assert tasks[0]["scope_complete"] is False
    assert tasks[0]["clarifications"][0]["required_from_retrieval"] is False
    assert [r["label"] for r in labels] == [1, 0]
    assert all(r["review_status"] == "pending" for r in labels)
    assert [p.read_bytes() for p in paths] == original


@pytest.mark.parametrize("change", ["feedback", "body", "unchecked_candidate"])
def test_rejects_stale_review_or_undecided_direct_text(tmp_path, change):
    paths = fixture_files(tmp_path)
    if change in {"feedback", "body"}:
        text = paths[1].read_text("utf-8")
        old = "accepted" if change == "feedback" else "original rule"
        paths[1].write_text(text.replace(old, "changed"), "utf-8")
    else:
        decisions = json.loads(paths[2].read_text("utf-8"))
        decisions["annotation_decisions"]["positive_chunk_orders"]["t"] = []
        paths[2].write_text(json.dumps(decisions), "utf-8")
    with pytest.raises(ValueError):
        apply_review(*paths, tmp_path / "out")
    assert not (tmp_path / "out").exists()
