import json
import runpy

import pytest

prepare = runpy.run_path("eval/kb/prepare_feature_tasks.py")["prepare"]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_tasks_bind_direct_source_and_keep_review_pending(tmp_path, split):
    snapshot = {"snapshot": {"snapshot_id": "s", "documents": [
        {"document_id": "doc", "version_id": "v", "content_sha256": "h"}]},
        "blocks": [{"block_id": "v:0", "document_id": "v", "origin": "text",
                    "kind": "paragraph", "content": "before target after", "locator": {}}],
        "chunks": [{"chunk_id": "c", "sources": [
            {"block_id": "v:0", "start_char": 0, "end_char": 19}]}]}
    seed = [{"task_id": "task", "feature": "f", "document_id": "doc", "request": "task",
        "in_scope": ["f"], "out_of_scope": [], "questions": ["unknown boundary"],
        "rules": [{"rule_id": "r", "statement": "draft", "anchors": [
            {"order": 0, "quote": "target"}]}]}]
    sp, tp = tmp_path / "snapshot.json", tmp_path / "seed.json"
    sp.write_text(json.dumps(snapshot), "utf-8")
    tp.write_text(json.dumps(seed), "utf-8")
    if split == "dev":
        prepare(sp, tp, tmp_path / "out")
    else:
        prepare(sp, tp, tmp_path / "out", split=split)
    result = json.loads((tmp_path / "out/tasks.draft.json").read_text("utf-8"))[0]
    assert result["split"] == split and result["review_status"] == "pending"
    assert result["formal_metrics"] is None and result["scope_complete"] is False
    evidence = result["rules"][0]["evidence"][0]
    assert (evidence["start_char"], evidence["end_char"]) == (7, 13)
    assert evidence["anchor_chunk_ids"] == ["c"]
    snapshot["blocks"][0]["deprecated_ranges"] = [{"start_char": 7, "end_char": 13}]
    sp.write_text(json.dumps(snapshot), "utf-8")
    with pytest.raises(ValueError, match="作废"):
        prepare(sp, tp, tmp_path / "blocked")


def test_invalid_split_refused_before_reading_files(tmp_path):
    with pytest.raises(ValueError, match="集合只能"):
        prepare(tmp_path / "missing", tmp_path / "missing", tmp_path / "out", split="unknown")
