import math

import pytest
from pydantic import BaseModel, ValidationError

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.bootstrap import bootstrap
from tc_rag.core.config import load_config
from tc_rag.infrastructure.providers.fakes import FakeEmbedder, FakeLLM


class Output(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_llm_outputs_failure_and_exhaustion():
    error = TimeoutError("fake")
    llm = FakeLLM([{"value": 3}, error, {"value": "invalid"}])
    result = await llm.generate({}, Output)
    assert result.output == Output(value=3)
    assert result.usage.total_tokens is None
    with pytest.raises(TimeoutError) as caught:
        await llm.generate({}, Output)
    assert caught.value is error
    with pytest.raises(ValidationError):
        await llm.generate({}, Output)
    with pytest.raises(RuntimeError, match="no configured response"):
        await llm.generate({}, Output)
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_embedding_determinism_order_and_normalization():
    space = EmbeddingSpace("fake", "demo", 8)
    first = await FakeEmbedder().embed_batch(["a", "b", "a"], space)
    second = await FakeEmbedder().embed_batch(["a", "b", "a"], space)
    assert first.vectors == second.vectors
    assert first.vectors[0] == first.vectors[2]
    assert first.vectors[0] != first.vectors[1]
    assert all(math.sqrt(sum(v * v for v in row)) == pytest.approx(1) for row in first.vectors)
    assert (await FakeEmbedder().embed_batch([], space)).vectors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vectors", [[[1.0]], [[0.0, 0.0]], [[float("nan"), 1.0]]])
async def test_invalid_embeddings(vectors):
    with pytest.raises(ValueError):
        await FakeEmbedder([vectors]).embed_batch(["a"], EmbeddingSpace("fake", "demo", 2))


@pytest.mark.asyncio
async def test_embedding_preset_and_fault():
    fake = FakeEmbedder([[[3.0, 4.0]], TimeoutError("fake")])
    result = await fake.embed_batch(["a"], EmbeddingSpace("fake", "demo", 2, normalize=False))
    assert result.vectors == [[3.0, 4.0]]
    with pytest.raises(TimeoutError):
        await fake.embed_batch(["a"], EmbeddingSpace("fake", "demo", 2))


def test_bootstrap_is_fake_only():
    container = bootstrap(load_config(environ={}))
    assert isinstance(container.llm, FakeLLM)
    assert isinstance(container.embedder, FakeEmbedder)
