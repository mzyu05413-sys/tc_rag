import json
import runpy

import pytest


def test_heldout_rejected_before_retrieval(tmp_path):
    run = runpy.run_path("eval/kb/run_dev_multik.py")["run"]
    (tmp_path / "questions.evidence_draft.json").write_text(
        json.dumps([{"split": "test"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="仅接受dev"):
        run(tmp_path, tmp_path / "missing", tmp_path / "missing", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_pending_labels_rejected(tmp_path):
    run = runpy.run_path("eval/kb/run_dev_multik.py")["run"]
    (tmp_path / "questions.evidence_draft.json").write_text(
        json.dumps([{"split": "dev", "query_id": "q"}]), encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([{"review_status": "pending"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="尚未批准"):
        run(tmp_path, tmp_path / "missing", labels, tmp_path / "out")
