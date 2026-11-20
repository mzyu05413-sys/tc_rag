import hashlib
import json
import runpy

import pytest

prepare = runpy.run_path("eval/kb/prepare_holdout_candidates.py")["prepare"]


def inputs(tmp_path, *, old_positive="old", origin="text"):
    snapshot = {"snapshot": {"snapshot_id": "s", "documents": [
        {"document_id": "new.docx", "version_id": "v", "content_sha256": "h"}]},
        "blocks": [{"block_id": "b", "document_id": "v", "origin": origin,
                    "kind": "paragraph", "content": "before target after", "locator": {}}],
        "chunks": [{"chunk_id": "c", "sources": [
            {"block_id": "b", "start_char": 0, "end_char": 19}]}]}
    spec = [{"query_id": "new", "module": "new", "query_family": "family",
             "document_id": "new.docx", "question": "new question",
             "reference_answer": "answer draft", "quotes": ["target"]}]
    old = [{"query_id": "old", "question": "old question", "snapshot_id": "s"}]
    labels = [{"snapshot_id": "s", "chunk_id": old_positive, "label": 1}]
    paths = []
    for name, payload in [("snapshot", snapshot), ("spec", spec), ("old", old), ("labels", labels)]:
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(payload), "utf-8")
        paths.append(path)
    return paths


def test_source_mapping_remains_pending_and_does_not_create_negative_labels(tmp_path):
    paths = inputs(tmp_path)
    prepare(*paths, tmp_path / "out")
    q = json.loads((tmp_path / "out/questions.draft.json").read_text("utf-8"))[0]
    evidence = q["evidence_candidates"][0]
    assert (evidence["start_char"], evidence["end_char"], evidence["quote"]) == (7, 13, "target")
    assert evidence["anchor_chunk_ids"] == ["c"]
    assert q["review_status"] == "pending" and q["scope_complete"] is False
    assert q["should_abstain"] is None
    assert not list((tmp_path / "out").glob("*labels*"))
    with pytest.raises(ValueError, match="已存在"):
        prepare(*paths, tmp_path / "out")


@pytest.mark.parametrize("kwargs", [{"old_positive": "c"}, {"origin": "vision"}])
def test_rejects_old_positive_overlap_or_vision_anchor(tmp_path, kwargs):
    paths = inputs(tmp_path, **kwargs)
    with pytest.raises(ValueError):
        prepare(*paths, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_official_anchor_is_hashed_and_does_not_auto_approve(tmp_path):
    paths = inputs(tmp_path)
    spec = json.loads(paths[1].read_text("utf-8"))
    spec[0].update(official_page="manual", official_anchor="official rule")
    paths[1].write_text(json.dumps(spec), "utf-8")
    manuals = tmp_path / "manuals"
    manuals.mkdir()
    text = "heading official rule end"
    (manuals / "manual.txt").write_text(text, "utf-8")
    (manuals / "manifest.json").write_text(json.dumps([{
        "name": "manual", "final_url": "https://fixture/manual",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "retrieved_utc": "2026-11-16T00:00:00Z"}]), "utf-8")
    prepare(*paths, tmp_path / "out", manuals)
    q = json.loads((tmp_path / "out/questions.draft.json").read_text("utf-8"))[0]
    ref = q["official_reference"]
    assert text[ref["start_char"]:ref["end_char"]] == "official rule"
    assert q["review_status"] == "pending" and q["scope_complete"] is False
    (manuals / "manual.txt").write_text("tampered", "utf-8")
    with pytest.raises(ValueError, match="指纹变化"):
        prepare(*paths, tmp_path / "bad", manuals)
