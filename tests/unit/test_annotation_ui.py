"""本地标注页：草稿隔离、材料绑定、来源保留和脚本注入保护。"""

import json
import re
from pathlib import Path

import pytest
from build_annotation_ui import build_payload, render_page


@pytest.fixture
def labels_dir(tmp_path: Path) -> Path:
    groups = {
        "groups": [
            {"group_id": "G001", "quote": "原文一", "corpus_ids": ["C001", "C002"]},
            {"group_id": "G002", "quote": "图片解释", "corpus_ids": ["C003"]},
        ]
    }
    labels = {
        "source_run_id": "run_fixture",
        "queries": [
            {
                "record_id": "Q001",
                "query_id": "query_fixture",
                "text": "当前查询",
                "stage": "plan_points",
                "relevant_groups": ["G001"],
                "irrelevant_groups": ["G002"],
                "reviewed_by": "user-via-chat",
                "review_scope": "presented_candidates",
                "corpus_review_complete": False,
            },
            {
                "record_id": "Q002",
                "query_id": "query_second",
                "text": "待审核查询",
                "stage": "generate",
                "relevant_groups": ["G002"],
                "hint_top10_groups": ["G001"],
                "reviewed_by": "draft-by-assistant",
            },
        ],
    }
    (tmp_path / "corpus_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "labels_by_group.json").write_text(json.dumps(labels), encoding="utf-8")
    return tmp_path


def test_payload_preserves_drafts_and_only_reads_sources(labels_dir):
    before = {path.name: path.read_bytes() for path in labels_dir.iterdir()}
    payload = build_payload(labels_dir)
    assert payload["queries"][0]["reviewed_by"] == "user-via-chat"
    assert payload["queries"][0]["corpus_review_complete"] is False
    assert payload["queries"][1]["reviewed_by"] == "draft-by-assistant"
    assert payload["groups"][0]["corpus_ids"] == ["C001", "C002"]
    assert before == {path.name: path.read_bytes() for path in labels_dir.iterdir()}


def test_dataset_identity_is_stable_but_seed_revision_tracks_reviews(labels_dir):
    before = build_payload(labels_dir)
    path = labels_dir / "labels_by_group.json"
    labels = json.loads(path.read_text(encoding="utf-8"))
    labels["queries"][0]["notes"] = "人工新增备注"
    path.write_text(json.dumps(labels), encoding="utf-8")
    after = build_payload(labels_dir)
    assert after["dataset_id"] == before["dataset_id"]
    assert after["seed_revision"] != before["seed_revision"]


def test_display_question_updates_preserve_cache_identity_and_actual_query(labels_dir):
    before = build_payload(labels_dir)
    questions = {
        "source_run_id": "run_fixture",
        "questions": [
            {"record_id": "Q001", "query_id": "query_fixture", "question": "哪些原文支持当前功能？"}
        ],
    }
    path = labels_dir / "query_questions.json"
    path.write_text(json.dumps(questions), encoding="utf-8")
    after = build_payload(labels_dir)
    assert after["dataset_id"] == before["dataset_id"]
    assert after["seed_revision"] == before["seed_revision"]
    assert after["queries"] == before["queries"]
    assert after["display_questions"] == {"Q001": "哪些原文支持当前功能？"}
    assert after["question_hints_fingerprint"] != before["question_hints_fingerprint"]


@pytest.mark.parametrize("damage", ["run", "query", "unknown", "duplicate", "empty"])
def test_display_questions_must_match_actual_query(labels_dir, damage):
    questions = {
        "source_run_id": "run_fixture",
        "questions": [
            {"record_id": "Q001", "query_id": "query_fixture", "question": "哪些原文相关？"}
        ],
    }
    item = questions["questions"][0]
    if damage == "run":
        questions["source_run_id"] = "another_run"
    elif damage == "query":
        item["query_id"] = "another_query"
    elif damage == "unknown":
        item["record_id"] = "Q999"
    elif damage == "duplicate":
        questions["questions"].append(item)
    else:
        item["question"] = " "
    (labels_dir / "query_questions.json").write_text(json.dumps(questions), encoding="utf-8")
    with pytest.raises(ValueError):
        build_payload(labels_dir)


@pytest.mark.parametrize("damage", ["unknown", "additional", "overlap", "duplicate", "full"])
def test_invalid_labels_are_rejected(labels_dir, damage):
    path = labels_dir / "labels_by_group.json"
    labels = json.loads(path.read_text(encoding="utf-8"))
    query = labels["queries"][0]
    if damage == "unknown":
        query["relevant_groups"] = ["G999"]
    elif damage == "additional":
        query["additional_review_groups"] = ["G999"]
    elif damage == "overlap":
        query["irrelevant_groups"] = ["G001"]
    elif damage == "duplicate":
        labels["queries"].append(query)
    else:
        query["irrelevant_groups"] = []
        query["corpus_review_complete"] = True
    path.write_text(json.dumps(labels), encoding="utf-8")
    with pytest.raises(ValueError):
        build_payload(labels_dir)


def test_corpus_source_requires_matching_run_and_preserves_vision(labels_dir, tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    manifest_path = dataset / "manifest.json"
    manifest_path.write_text(json.dumps({"source_run_id": "run_fixture"}), encoding="utf-8")
    corpus = [
        {"corpus_id": cid, "origin": "vision" if cid == "C003" else "text", "locator": {}}
        for cid in ("C001", "C002", "C003")
    ]
    (dataset / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    payload = build_payload(labels_dir, dataset)
    assert payload["groups"][1]["sources"][0]["origin"] == "vision"
    manifest_path.write_text(json.dumps({"source_run_id": "another_run"}), encoding="utf-8")
    with pytest.raises(ValueError, match="不属于同一次运行"):
        build_payload(labels_dir, dataset)


def test_embedded_document_content_cannot_close_script_or_become_markup(labels_dir):
    payload = build_payload(labels_dir)
    dangerous = '</script><script>alert("document instruction")</script>&\u2028'
    payload["groups"][0]["quote"] = dangerous
    page = render_page(payload)
    match = re.search(
        r'<script type="application/json" id="annotation-data">(.*?)</script>', page, re.S
    )
    assert match is not None
    assert "<" not in match[1] and "&" not in match[1]
    assert json.loads(match[1])["groups"][0]["quote"] == dangerous
    assert "connect-src 'none'" in page
    assert "src=\"http" not in page


def test_template_requires_exactly_one_placeholder(labels_dir, tmp_path):
    template = tmp_path / "bad.html"
    template.write_text("missing marker", encoding="utf-8")
    with pytest.raises(ValueError, match="占位符"):
        render_page(build_payload(labels_dir), template)
