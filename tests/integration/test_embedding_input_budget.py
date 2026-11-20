"""真实 SDK 加 MockTransport 验证文本预算切批，禁止超长文本进入请求。"""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder, input_byte_limit


@pytest.mark.asyncio
async def test_embedding_budget_packs_requests_and_rejects_oversize():
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload["input"])
        return httpx.Response(200, json={
            "object": "list", "model": "fixture",
            "data": [{"object": "embedding", "index": i, "embedding": [1.0, 0.0]}
                     for i in range(len(payload["input"]))],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider = OpenAIEmbedder(
        Endpoint(model="fixture", max_input_bytes=32), Runtime(), "run", lambda record: None,
        client=client,
    )
    space = EmbeddingSpace("fixture", "fixture", 2)
    try:
        result = await provider.embed_batch(["中" * 8] * 3, space)
        assert len(result.vectors) == 3
        assert len(requests) == 3
        assert all(sum(len(t.encode("utf-8")) for t in batch) <= 32 for batch in requests)
        with pytest.raises(ValueError, match="超出字节预算"):
            await provider.embed_batch(["中" * 12], space)
        assert len(requests) == 3
    finally:
        await provider.close()
    assert input_byte_limit(Endpoint(model="embedding-3")) == 3000
    assert input_byte_limit(Endpoint(model="fixture")) is None
