"""关键词算法装配（A8）：RagContext 在 tf/BM25 之间二选一，默认 tf 保留历史基线。"""

import pytest

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.core.config import Retrieval
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.retrieval.bm25 import BM25Index
from tc_rag.infrastructure.retrieval.context import RagContext
from tc_rag.infrastructure.retrieval.keyword import KeywordIndex
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


def _context(tmp_path, **kwargs):
    repo = SqliteRepository(tmp_path / "db")
    store = LocalArtifactStore(tmp_path / "files")
    return RagContext(
        repo, store, FakeEmbedder(), EmbeddingSpace("fixture", "m", 2),
        "cosine", 5, "run", **kwargs,
    )


def test_keyword_scorer_defaults_to_tf(tmp_path):
    pytest.importorskip("jieba")
    context = _context(tmp_path, mode="hybrid")
    assert isinstance(context.keyword, KeywordIndex)
    assert not isinstance(context.keyword, BM25Index)


def test_keyword_scorer_selects_bm25(tmp_path):
    pytest.importorskip("jieba")
    context = _context(tmp_path, mode="hybrid", keyword_scorer="bm25")
    assert isinstance(context.keyword, BM25Index)


def test_keyword_mode_builds_keyword_index(tmp_path):
    pytest.importorskip("jieba")
    assert _context(tmp_path, mode="keyword").keyword is not None


def test_vector_mode_keeps_keyword_none(tmp_path):
    assert _context(tmp_path).keyword is None


def test_keyword_scorer_rejects_unknown(tmp_path):
    pytest.importorskip("jieba")
    with pytest.raises(ValueError, match="关键词算法"):
        _context(tmp_path, mode="hybrid", keyword_scorer="unknown")


def test_retrieval_config_defaults_and_snapshot_compat():
    config = Retrieval()
    assert config.keyword_scorer == "tf"
    assert config.mode == "vector"
    assert config.rrf_vector_weight == 1.0
    assert config.rerank_enabled is True
    # 默认值不进入快照，保持旧运行配置快照形状不变。
    dumped = config.model_dump(mode="json")
    assert "keyword_scorer" not in dumped
    assert "mode" not in dumped
    assert "rrf_constant" not in dumped
    assert "rrf_vector_weight" not in dumped
    assert "rerank_enabled" not in dumped
    assert Retrieval(keyword_scorer="bm25").model_dump(mode="json")["keyword_scorer"] == "bm25"
    assert Retrieval(mode="hybrid").model_dump(mode="json")["mode"] == "hybrid"
    assert Retrieval(rrf_vector_weight=2.0).model_dump(mode="json")["rrf_vector_weight"] == 2.0
    assert Retrieval(rerank_enabled=False).model_dump(mode="json")["rerank_enabled"] is False
