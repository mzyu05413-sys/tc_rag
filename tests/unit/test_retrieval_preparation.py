"""用虚构运行验证实验材料成功导出、来源一致与失败不覆盖。"""

import hashlib
import json
import sqlite3

import numpy as np
import pytest

from eval.prepare_retrieval import checked_vector, prepare, query_candidates
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock, EvidenceSpan, Locator, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.context import RagContext
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


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


@pytest.fixture
async def prepared_database(tmp_path):
    """真实 SQLite/缓存/索引写法，向量只来自 Fake，不需要服务或密钥。"""
    from tc_rag.application.orchestration.pipeline import Pipeline
    from tc_rag.application.ports.contracts import EmbeddingSpace
    from tc_rag.core.config import AppConfig

    database = tmp_path / "fixture.sqlite"
    repository = SqliteRepository(database)
    config = AppConfig()
    config.models.embedding.model = "fixture-embedding"
    config.retrieval.embedding_dimension = 3
    config.generation.batch_point_limit = 2
    run_id = "fixture_materials"
    repository.save(
        "runs", run_id,
        Run(run_id=run_id, status="partial_failed", config_snapshot=config.model_dump()),
    )

    def snapshot(stage, payload):
        key = f"{run_id}:{stage}"
        repository.save(
            "stage_snapshots", key,
            StageSnapshot(snapshot_id=key, run_id=run_id, stage=stage, payload=payload),
        )

    text = "单批最多300个对象，对象数量为整数。"
    block = DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content=text, locator=Locator(paragraph_index=0),
    )
    evidence = EvidenceSpan(
        evidence_id="e", block_id="b", quote="单批最多300个对象", locator=block.locator,
    )
    chunk = Chunk(
        chunk_id="c", text=text, token_estimate=20, fingerprint=fingerprint(text),
        sources=[ChunkSource(block_id="b", start_char=0, end_char=len(text))],
    )
    rule = "单批最多300个对象"
    instruction = "生成功能测试点"
    point = "检查数量上限"
    snapshot("input", {"instruction": instruction, "document_fingerprint": "document-hash"})
    snapshot("chunk", {"blocks": [block.model_dump()], "chunks": [chunk.model_dump()]})
    snapshot("extract", {"evidence": [evidence.model_dump()], "rules": [{"statement": rule}]})
    snapshot("plan_points", {"points": [
        {"objective": point, "status": "ready", "applicability": "applicable"},
        {"objective": "规则待澄清", "status": "needs_clarification",
         "applicability": "undetermined"},
    ]})
    context = RagContext(
        repository, LocalArtifactStore(tmp_path / "assets"), FakeEmbedder(),
        EmbeddingSpace("fixture", "fixture-embedding", 3), "cosine", 20, run_id,
    )
    await context.prepare(
        "document-hash", Pipeline._retrieval_evidence([evidence], [block], [chunk]),
    )
    bundles = []
    for stage, query in (("plan_points", instruction + "\n" + rule), ("generate", point)):
        bundle = await context.retrieve(query, {"e"})
        bundle["generation"] = {"stage": stage}
        bundles.append(bundle)
    snapshot("retrieval", {"bundles": bundles})
    return database, run_id


def change_json_record(database, table, key, change):
    """只用于独立夹具制造损坏；不会读取用户运行数据。"""
    assert table in {"stage_snapshots", "embedding_records", "index_manifests", "embedding_spaces"}
    with sqlite3.connect(database) as connection:
        row = connection.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
        value = json.loads(row[0])
        change(value)
        connection.execute(
            f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(value), key),
        )


def record(database, table, key):
    with sqlite3.connect(database) as connection:
        return json.loads(connection.execute(
            f"SELECT payload FROM {table} WHERE id=?", (key,),
        ).fetchone()[0])


def test_complete_preparation_preserves_source_and_outputs_auditable_materials(
    prepared_database, tmp_path,
):
    database, run_id = prepared_database
    original = database.read_bytes()
    output = tmp_path / "materials"
    result = prepare(database, run_id, output)
    assert database.read_bytes() == original
    assert result["source_status"] == "partial_failed"
    assert result["network_requests"] == 0
    assert result["corpus_count"] == 2 and result["query_count"] == 2
    assert result["raw_vector_experiment_available"] is False
    queries = json.loads((output / "queries.json").read_text(encoding="utf-8"))
    assert [query["stage"] for query in queries] == ["plan_points", "generate"]
    assert all(fingerprint(query["text"]) == query["query_id"] for query in queries)
    labels = json.loads((output / "labels.template.json").read_text(encoding="utf-8"))
    assert all(query["relevant_corpus_ids"] is None for query in labels["queries"])
    assert not (output / "labels.json").exists()
    corpus = np.load(output / "corpus_vectors.npy", allow_pickle=False)
    cached = np.load(output / "corpus_vectors_cached.npy", allow_pickle=False)
    assert corpus.shape == cached.shape == (2, 3)
    assert corpus.dtype == np.float32 and cached.dtype == np.float64
    np.testing.assert_array_equal(corpus, cached.astype(np.float32))
    assert np.load(output / "query_vectors.npy", allow_pickle=False).shape == (2, 3)
    for name, checksum in result["files_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == checksum
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == result
    assert "密钥" not in (output / "queries.json").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="输出目录已存在"):
        prepare(database, run_id, output)


@pytest.mark.parametrize("damage,match", [
    ("missing", "缺少实验材料"),
    ("objects", "对象集合不一致"),
    ("cache", "向量缓存"),
    ("index", "运行配置、历史索引与向量空间不一致"),
    ("matrix", "矩阵与历史索引版本不一致"),
    ("query", "历史查询文字无法"),
    ("bundle", "历史检索混用"),
    ("scope", "历史检索混用"),
    ("direct", "直接证据超出固定语料范围"),
    ("space", "运行配置、历史索引与向量空间不一致"),
    ("empty", "没有可用的历史检索查询"),
])
def test_invalid_historical_materials_do_not_create_output(
    prepared_database, tmp_path, damage, match,
):
    database, run_id = prepared_database
    if damage == "missing":
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM stage_snapshots WHERE id=?", (f"{run_id}:extract",))
    elif damage == "objects":
        change_json_record(database, "stage_snapshots", f"{run_id}:extract", lambda value:
                           value["payload"]["evidence"].clear())
    elif damage == "cache":
        with sqlite3.connect(database) as connection:
            key = connection.execute(
                "SELECT id FROM embedding_records ORDER BY id LIMIT 1"
            ).fetchone()[0]
        change_json_record(database, "embedding_records", key, lambda value:
                           value["payload"].update(vector_fingerprint="broken"))
    elif damage == "index":
        binding = record(database, "stage_snapshots", f"{run_id}:index")["payload"]
        change_json_record(database, "index_manifests", binding["manifest_id"], lambda value:
                           value["payload"].update(version="old-version"))
    elif damage == "matrix":
        corpus = record(database, "stage_snapshots", f"{run_id}:extract")["payload"]
        binding = record(database, "stage_snapshots", f"{run_id}:index")["payload"]
        quote = corpus["evidence"][0]["quote"]
        key = "embedding_" + fingerprint([binding["space_id"], quote])
        change_json_record(database, "embedding_records", key, lambda value:
                           value["payload"].update(
                               vector=[1.0, 0.0, 0.0],
                               vector_fingerprint=fingerprint([1.0, 0.0, 0.0]),
                           ))
    elif damage in {"query", "bundle", "scope", "direct", "empty"}:
        def mutate(value):
            bundles = value["payload"]["bundles"]
            if damage == "empty":
                bundles.clear()
            elif damage == "direct":
                bundles[0]["direct_ids"] = ["foreign-evidence"]
            else:
                key = {"query": "query_id", "bundle": "index_version", "scope": "scope"}[damage]
                bundles[0][key] = "changed"
        change_json_record(database, "stage_snapshots", f"{run_id}:retrieval", mutate)
    elif damage == "space":
        binding = record(database, "stage_snapshots", f"{run_id}:index")["payload"]
        change_json_record(database, "embedding_spaces", binding["space_id"], lambda value:
                           value["payload"].update(model="another-model"))
    output = tmp_path / "rejected-materials"
    with pytest.raises(ValueError, match=match):
        prepare(database, run_id, output)
    assert not output.exists()


def test_public_local_example_matches_current_config_without_credentials():
    from pathlib import Path

    from tc_rag.core.config import load_config

    config = load_config(Path("workdir/local.example.yaml"), environ={})
    assert config.parsing.scan_policy == "report_unsupported"
    assert config.parsing.infer_docx_headings is False
    assert config.export.point_tree_layout == "module"
    assert all(getattr(config.models, role).api_key is None
               for role in ("llm", "embedding", "vision", "rerank"))
