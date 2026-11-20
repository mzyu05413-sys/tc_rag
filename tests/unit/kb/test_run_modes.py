import hashlib
import json
import runpy
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.domain.identity import fingerprint
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.retrieval.keyword import KeywordIndex


def test_offline_modes_and_mismatched_query(tmp_path):
    run = runpy.run_path(str(Path("eval/kb/run_modes.py")))["run"]
    # 默认离线测试不依赖可选 jieba；仍运行真实词频检索与 RRF。
    run.__globals__["KeywordIndex"] = lambda: KeywordIndex(tokenizer=str.split)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    space = EmbeddingSpace("fake", "fake", 2)
    index = NumpyIndex()
    index.build(["c"], [[1., 0.]], space)
    snapshot = {"snapshot": {"snapshot_id": "s", "embedding_space_id": fingerprint(asdict(space)),
                             "documents": [{"document_id": "d", "version_id": "v"}]},
                "metric": "cosine", "index_version": index.version,
                "blocks": [{"block_id": "b", "document_id": "v"}],
                "chunks": [{"chunk_id": "c", "text": "approval",
                            "sources": [{"block_id": "b"}]}]}
    def write(name, value):
        (bundle / name).write_text(json.dumps(value), encoding="utf-8")
    write("snapshot.json", snapshot)
    write("chunk_ids.json", ["c"])
    write("questions.evidence_draft.json", [
        {"query_id": "q", "question": "approval", "document_ids": ["d"]}])
    np.save(bundle / "document_vectors.npy", index.vectors)
    write("manifest.partial.json", {"files": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.iterdir()}})
    queries = tmp_path / "queries.json"
    payload = {"space": asdict(space), "snapshot_id": "s", "queries": [
        {"query_id": "q", "question": "approval", "vector": [1., 0.]}]}
    queries.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "out"
    run(bundle, queries, out)
    rows = json.loads((out / "candidates.json").read_text(encoding="utf-8"))
    assert {r["mode"] for r in rows} == {"vector", "keyword", "hybrid"}
    assert all(r["candidates"][0]["chunk_id"] == "c" for r in rows)
    assert all(r["formal_metrics"] is None for r in rows)
    filtered = tmp_path / "filtered"
    run(bundle, queries, filtered, filter_punctuation=True)
    filtered_rows = json.loads((filtered / "candidates.json").read_text(encoding="utf-8"))
    assert all(r["candidates"][0]["chunk_id"] == "c" for r in filtered_rows)
    assert json.loads((filtered / "complete.json").read_text())["token_filter"] == (
        "unicode_punctuation_v1")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        run(bundle, queries, out)
    payload["queries"][0]["question"] = "changed"
    queries.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="问题文字"):
        run(bundle, queries, tmp_path / "other")
