import json
import runpy

import pytest


def test_unapproved_gold_rejected(tmp_path):
    score = runpy.run_path("eval/kb/score_text_pilot.py")["score"]
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"review_status": "pending"}]))
    with pytest.raises(ValueError, match="已批准"):
        score(tmp_path, gold, tmp_path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_existing_scores_not_overwritten(tmp_path):
    score = runpy.run_path("eval/kb/score_text_pilot.py")["score"]
    with pytest.raises(ValueError, match="已存在"):
        score(tmp_path, tmp_path / "missing", tmp_path, tmp_path)
