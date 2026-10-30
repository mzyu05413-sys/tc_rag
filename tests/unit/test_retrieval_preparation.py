import sqlite3

import pytest

from eval.prepare_retrieval import checked_vector, prepare, query_candidates
from tc_rag.domain.identity import fingerprint


def test_query_reconstruction_uses_original_order_and_supports_split_batches():
    stages = {
        "input": {"instruction": "生成目标"},
        "extract": {"rules": [{"statement": "规则一"}, {"statement": "规则二"}]},
        "plan_points": {"points": [
            {"objective": "目标一", "status": "ready", "applicability": "applicable"},
            {"objective": "不应入查询", "status": "needs_clarification",
             "applicability": "undetermined"},
            {"objective": "目标二", "status": "ready", "applicability": "applicable"},
        ]},
    }
    found = query_candidates(stages, 2)
    for text in ("生成目标\n规则一\n规则二", "目标一\n目标二", "目标一", "目标二"):
        assert found[fingerprint(text)] == text
    assert fingerprint("不应入查询") not in found
    with pytest.raises(ValueError):
        query_candidates(stages, 0)


@pytest.mark.parametrize("damage", [None, "space", "text", "fingerprint", "dimension", "nan"])
def test_vector_cache_integrity_is_required(damage):
    data = {"space_id": "s", "text_fingerprint": fingerprint("text"),
            "vector": [0.6, 0.8], "vector_fingerprint": fingerprint([0.6, 0.8])}
    if damage == "space":
        data["space_id"] = "other"
    elif damage == "text":
        data["text_fingerprint"] = fingerprint("other")
    elif damage == "fingerprint":
        data["vector_fingerprint"] = "broken"
    elif damage == "dimension":
        data["vector"] = [0.6]
        data["vector_fingerprint"] = fingerprint([0.6])
    elif damage == "nan":
        data["vector"] = [float("nan"), 0.8]
    if damage:
        with pytest.raises(ValueError):
            checked_vector(data, "text", "s", 2)
    else:
        assert checked_vector(data, "text", "s", 2) == [0.6, 0.8]


def test_preparation_never_overwrites_existing_labels_or_creates_source_database(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    labels = output / "labels.json"
    labels.write_text("人工标注", encoding="utf-8")
    database = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="输出目录已存在"):
        prepare(database, "r", output)
    assert labels.read_text(encoding="utf-8") == "人工标注"
    with pytest.raises(sqlite3.OperationalError):
        prepare(database, "r", tmp_path / "new")
    assert not database.exists()
