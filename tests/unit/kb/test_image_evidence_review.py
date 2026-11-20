import hashlib
import json
import runpy
from pathlib import Path

import pytest

build = runpy.run_path(str(Path("eval/kb/build_image_evidence_review.py")))["build"]


def material(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"fake-image-for-hash-test")
    text = ("【图片可见事实】\n- f1 [overview] 有编辑按钮。\n"
            "【正文关联解释】\n- f2 正文补充权限限制。")
    row = dict(image_key="i", copied_path=str(image),
               image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
               vision_text=text, vision_block_id="v", task_id="q",
               vision_chunks=[dict(chunk_id="c", sources=[dict(
                   block_id="v", start_char=0, end_char=len(text))])])
    decision = dict(title="<unsafe>", proposed_relevance=1, fact_ids=["f1"],
                    rules=["edit"], visible="有按钮", limit="不证明权限")
    (tmp_path / "image_inventory.json").write_text(json.dumps(
        dict(snapshot_id="s", images=[row])), "utf-8")
    (tmp_path / "audit_decisions.json").write_text(json.dumps(dict(i=decision)), "utf-8")
    return row, decision


def test_fact_span_matches_frozen_chunk_and_keeps_pending(tmp_path):
    material(tmp_path)
    build(tmp_path)
    payload = json.loads((tmp_path / "evidence.prelabel.json").read_text("utf-8"))
    row = payload["images"][0]
    span = row["fact_spans"][0]
    assert row["vision_text"][span["start_char"]:span["end_char"]] == span["quote"]
    assert span["chunks"][0]["complete_fact"] is True
    assert row["review_status"] == "pending" and payload["formal_metrics"] is None
    page = (tmp_path / "打开审核.html").read_text("utf-8")
    assert "&lt;unsafe&gt;" in page and "1张图片" in page
    with pytest.raises(ValueError, match="输出已存在"):
        build(tmp_path)


def test_context_restatement_cannot_be_visible_fact(tmp_path):
    _, decision = material(tmp_path)
    decision["fact_ids"] = ["f2"]
    (tmp_path / "audit_decisions.json").write_text(json.dumps(dict(i=decision)), "utf-8")
    with pytest.raises(ValueError, match="可见事实ID"):
        build(tmp_path)
    assert not (tmp_path / "evidence.prelabel.json").exists()


def test_tampered_original_image_rejected(tmp_path):
    material(tmp_path)
    (tmp_path / "image.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="图片哈希"):
        build(tmp_path)


def test_fact_in_other_source_cannot_match(tmp_path):
    row, _ = material(tmp_path)
    row["vision_chunks"][0]["sources"][0]["block_id"] = "different"
    (tmp_path / "image_inventory.json").write_text(json.dumps(
        dict(snapshot_id="s", images=[row])), "utf-8")
    with pytest.raises(ValueError, match="未映射冻结候选"):
        build(tmp_path)
