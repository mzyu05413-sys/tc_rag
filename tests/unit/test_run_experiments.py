"""实验运行器的成功路径与失配保护：用虚构材料包验证，不触碰真实数据。"""

import json
from pathlib import Path

import numpy as np
import pytest
from run_experiments import load_dataset, load_labels, run_e1, run_e2, run_e5


def _write(root: Path, name: str, payload: object) -> None:
    (root / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    root.mkdir()
    corpus = [
        {"corpus_id": "C001", "object_id": "o1", "quote": "甲"},
        {"corpus_id": "C002", "object_id": "o2", "quote": "乙"},
        {"corpus_id": "C003", "object_id": "o3", "quote": "丙"},
        {"corpus_id": "C004", "object_id": "o4", "quote": "丁"},
    ]
    queries = [
        {
            "record_id": "Q001",
            "query_id": "q1",
            "stage": "plan_points",
            "text": "查询一",
            "direct_ids": [],
        }
    ]
    matrix = np.array(
        [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    np.save(root / "corpus_vectors.npy", matrix.astype(np.float32))
    np.save(root / "corpus_vectors_cached.npy", matrix)
    np.save(root / "query_vectors.npy", np.array([[1.0, 0.0, 0.0]], dtype=np.float64))
    _write(root, "corpus.json", corpus)
    _write(root, "queries.json", queries)
    _write(root, "manifest.json", {"normalize": True, "corpus_count": 4})
    _write(
        root,
        "rerank_history.json",
        [
            {
                "query_id": "q1",
                "record_id": "Q001",
                "vector_hits": [
                    {"object_id": "o2", "rank": 1},
                    {"object_id": "o1", "rank": 2},
                    {"object_id": "o3", "rank": 3},
                    {"object_id": "o4", "rank": 4},
                ],
                "hits": [
                    {"object_id": "o1", "rank": 1},
                    {"object_id": "o2", "rank": 2},
                    {"object_id": "o3", "rank": 3},
                    {"object_id": "o4", "rank": 4},
                ],
                "rerank": {"status": "succeeded", "scores": {"o1": 0.9}},
                "timings_ms": {"rerank_ms": 12.5},
            }
        ],
    )
    return root


@pytest.fixture
def labels_file(tmp_path: Path) -> Path:
    path = tmp_path / "labels.json"
    _write(
        tmp_path,
        "labels.json",
        {
            "source_run_id": "run_fixture",
            "judgment": "binary",
            "queries": [
                {
                    "record_id": "Q001",
                    "relevant_corpus_ids": ["C001"],
                    "reviewed_by": "fixture",
                }
            ],
        },
    )
    return path


def test_e2_ranks_and_scores_normalized_groups(dataset_dir, labels_file, tmp_path):
    dataset = load_dataset(dataset_dir)
    labels = load_labels(labels_file, dataset)
    payload = run_e2(dataset, labels, tmp_path / "out")
    groups = {item["group"]: item for item in payload["groups"]}
    assert groups["C1"]["rankings"]["Q001"][0] == "o1"
    assert groups["C1"]["recall"]["@1"]["value"] == 1.0
    assert groups["C1"]["mrr@10"]["value"] == 1.0
    # 单位向量下 C1/C3/C5 必须同序。
    assert payload["correctness"]["unit_vector_top5_identical"] is True
    # 六组始终可跑：缺补采原始向量时退化为缓存向量，来源在 base_vectors 里标注。
    assert {item["status"] for item in payload["groups"]} == {"done"}
    assert payload["base_vectors"] == "cached_normalized"
    # 余弦内部会单位化，因此 C1/C2 排序必须一致。
    assert groups["C1"]["rankings"] == groups["C2"]["rankings"]
    assert payload["norm_check"]["status"] == "not_available"


def test_e5_compares_before_and_after_rerank(dataset_dir, labels_file, tmp_path):
    dataset = load_dataset(dataset_dir)
    labels = load_labels(labels_file, dataset)
    payload = run_e5(dataset, labels, tmp_path / "out")
    assert payload["candidate_set_changed"] == []
    assert payload["before"]["summary"]["mrr@10"]["value"] == pytest.approx(0.5)
    assert payload["after"]["summary"]["mrr@10"]["value"] == 1.0
    assert payload["movements"]["improved_queries"] == 1
    assert payload["rerank_status"] == {"succeeded": 1}


def test_unknown_label_corpus_id_is_rejected(dataset_dir, tmp_path):
    dataset = load_dataset(dataset_dir)
    path = tmp_path / "bad.json"
    _write(
        tmp_path,
        "bad.json",
        {"queries": [{"record_id": "Q001", "relevant_corpus_ids": ["C999"]}]},
    )
    with pytest.raises(ValueError, match="不存在的语料 ID"):
        load_labels(path, dataset)


def test_vector_row_mismatch_is_rejected(dataset_dir, tmp_path):
    np.save(dataset_dir / "corpus_vectors.npy", np.zeros((3, 3), dtype=np.float32))
    with pytest.raises(ValueError, match="语料向量行数"):
        load_dataset(dataset_dir)


def test_unlabeled_queries_produce_null_metrics(dataset_dir, tmp_path):
    dataset = load_dataset(dataset_dir)
    labels = {"Q001": None}
    payload = run_e5(dataset, labels, tmp_path / "out")
    assert payload["after"]["summary"]["recall@5"] == {
        "value": None,
        "judged": 0,
        "skipped": 1,
    }
    assert payload["movements"]["improved_queries"] == 0


def test_e1_reports_norm_deviation_and_zero_vectors(dataset_dir, tmp_path):
    raw = dataset_dir / "raw"
    raw.mkdir()
    corpus = np.array(
        [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.0, 0.0]],
        dtype=np.float32,
    )
    np.save(raw / "corpus_raw.npy", corpus)
    np.save(raw / "query_raw.npy", np.array([[1.0, 0.0, 0.0]], dtype=np.float64))
    _write(
        raw,
        "manifest.json",
        {
            "vector_origin": "resampled_raw_provider_output",
            "normalize": False,
            "model": "fixture",
            "usage": {"calls": 1},
        },
    )
    payload = run_e1(load_dataset(dataset_dir), tmp_path / "out")
    stats = payload["sets"]["corpus"]
    assert payload["status"] == "measured"
    assert stats["count"] == 4
    # 2.0、0.5 与零向量都偏离 1 超过 1%；零向量另行单列，不静默剔除。
    assert stats["deviating_1pct"] == 3
    assert stats["zero_vectors"] == 1
    assert payload["sets"]["query"]["deviating_1pct"] == 0
