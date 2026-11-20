"""固定矩阵约束新增知识库复用的原索引行为。"""

import hashlib

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore


def test_shared_index_snapshot_filter_and_order(tmp_path):
    index = NumpyIndex("cosine")
    space = EmbeddingSpace("fixture", "baseline", 2)
    index.build(["approval:a", "order:b", "approval:c"], [[1, 0], [0, 1], [-1, 0]], space)
    hits = index.search([1, 0], 3)
    assert [(hit.object_id, hit.rank, hit.raw_score) for hit in hits] == [
        ("approval:a", 1, 1.0), ("order:b", 2, 0.0), ("approval:c", 3, -1.0),
    ]
    assert [hit.object_id for hit in index.search([1, 0], 1, {"order:b"})] == ["order:b"]
    store = LocalArtifactStore(tmp_path / "assets")
    asset = index.publish(store)
    assert hashlib.sha256(store.read(asset)).hexdigest() == asset.checksum
    restored = NumpyIndex("cosine")
    restored.load(store, asset, index.ids, space, index.version)
    assert restored.search([1, 0], 3) == hits
