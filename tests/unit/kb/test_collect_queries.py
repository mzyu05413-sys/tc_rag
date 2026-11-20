import asyncio
import runpy

import pytest

from tc_rag.application.ports.contracts import EmbeddingBatch, EmbeddingSpace


def test_query_batch_validation():
    validate = runpy.run_path("eval/kb/collect_queries.py")["validate_batch"]
    space = EmbeddingSpace("fake", "fake", 2)
    validate(EmbeddingBatch([[1., 0.]], space), space, 1)
    for vectors in [[], [[float("nan"), 0.]], [[0., 0.]], [[1.]]]:
        with pytest.raises(ValueError):
            validate(EmbeddingBatch(vectors, space), space, 1)


def test_existing_output_rejected_before_config_or_provider(tmp_path):
    collect = runpy.run_path("eval/kb/collect_queries.py")["collect"]
    out = tmp_path / "queries.json"
    out.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        asyncio.run(collect(tmp_path, out, tmp_path / "absent", tmp_path, True))
    assert out.read_text(encoding="utf-8") == "keep"
