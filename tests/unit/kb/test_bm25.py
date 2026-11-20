import pytest

from tc_rag.infrastructure.retrieval.bm25 import BM25Index


def test_length_normalization_and_filter():
    index = BM25Index(str.split)
    index.build({"short": "approval", "long": "approval " + "noise " * 10})
    assert index.search("approval", 2, {"short", "long"}) == ["short", "long"]
    assert index.search("approval", 2, {"long"}) == ["long"]
    assert index.search("unknown", 2, {"short"}) == []


def test_rebuild_and_parameters():
    index = BM25Index(str.split)
    index.build({"a": "x"})
    index.build({})
    assert index.search("x", 1, {"a"}) == []
    with pytest.raises(ValueError):
        BM25Index(str.split, k1=0)


def test_rare_term_beats_common_term():
    index = BM25Index(str.split, b=0)
    index.build({"rare": "specific", "common1": "general", "common2": "general"})
    assert index.search("specific general", 3, set(index.documents))[0] == "rare"
