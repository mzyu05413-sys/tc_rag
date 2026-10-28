"""离线验证向量 HTTP 批次调度、全局并发、预算和取消收尾。"""

import asyncio
import json
from collections import Counter

import httpx
import pytest
from openai import AsyncOpenAI

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.application.ports.errors import EmbeddingProviderError
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder

SPACE = EmbeddingSpace("fixture", "m", 2, False)


def response(texts):
    # 特意颠倒 HTTP 响应顺序，验证批内 index 与批间原始顺序都恢复。
    return httpx.Response(
        200,
        json={
            "model": "m",
            "data": [
                {"index": i, "embedding": [float(text) + 1, 1.0]}
                for i, text in reversed(list(enumerate(texts)))
            ],
            "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
        },
    )


def adapter_for(handler, records, *, concurrency=2, batch_size=2, runtime=None):
    client = AsyncOpenAI(
        api_key="fake",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIEmbedder(
        Endpoint(model="m", concurrency=concurrency, batch_size=batch_size),
        runtime or Runtime(),
        "run",
        records.append,
        client,
    )


@pytest.mark.asyncio
async def test_one_large_call_parallelizes_batches_and_preserves_order():
    second_completed = asyncio.Event()
    completion_order = []
    records = []

    async def handler(request):
        texts = json.loads(request.content)["input"]
        if texts[0] == "0":
            # 原实现逐批串行会在这里超时，而不是靠时间阈值猜测是否并行。
            await asyncio.wait_for(second_completed.wait(), timeout=2)
        elif texts[0] == "2":
            second_completed.set()
        completion_order.append(texts[0])
        return response(texts)

    adapter = adapter_for(handler, records)
    try:
        result = await adapter.embed_batch([str(i) for i in range(5)], SPACE)
        assert result.vectors == [[float(i) + 1, 1.0] for i in range(5)]
        assert completion_order.index("2") < completion_order.index("0")
        assert result.usage.total_tokens == 5
        assert adapter.calls == len(records) == 3
        assert all(record.error is None for record in records)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_multiple_logical_calls_share_endpoint_concurrency():
    active = peak = 0
    records = []

    async def handler(request):
        nonlocal active, peak
        texts = json.loads(request.content)["input"]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return response(texts)
        finally:
            active -= 1

    adapter = adapter_for(handler, records)
    try:
        results = await asyncio.gather(
            adapter.embed_batch([str(i) for i in range(7)], SPACE),
            adapter.embed_batch([str(i) for i in range(7, 14)], SPACE),
        )
        assert peak == 2 and active == 0
        assert [vector for result in results for vector in result.vectors] == [
            [float(i) + 1, 1.0] for i in range(14)
        ]
        assert [result.usage.total_tokens for result in results] == [7, 7]
        assert len(records) == 8
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "provider"])
async def test_failure_drains_inflight_batches_and_does_not_start_queue(failure):
    both_started = asyncio.Event()
    active = calls = 0
    records = []

    async def handler(request):
        nonlocal active, calls
        texts = json.loads(request.content)["input"]
        active += 1
        calls += 1
        if calls == 2:
            both_started.set()
        try:
            await both_started.wait()
            if failure == "provider" and texts[0] == "0":
                return httpx.Response(400, json={"error": {"message": "private"}})
            await asyncio.Event().wait()
        finally:
            active -= 1

    adapter = adapter_for(handler, records)
    task = asyncio.create_task(adapter.embed_batch([str(i) for i in range(12)], SPACE))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=2)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert all(record.error == "CancelledError" for record in records)
        else:
            with pytest.raises(EmbeddingProviderError) as caught:
                await task
            assert caught.value.code == "HTTP_400"
            assert {record.error for record in records} == {"HTTP_400", "CancelledError"}
        assert active == 0 and calls == len(records) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
async def test_concurrent_batches_keep_request_budget():
    requests = []
    records = []

    async def handler(request):
        texts = json.loads(request.content)["input"]
        requests.append(texts)
        await asyncio.sleep(0.01)
        return response(texts)

    adapter = adapter_for(handler, records, runtime=Runtime(budget={"max_calls": 3}))
    try:
        with pytest.raises(RuntimeError, match="预算"):
            await adapter.embed_batch([str(i) for i in range(20)], SPACE)
        assert adapter.calls == len(requests) == len(records) == 3
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_retry_repeats_only_failed_batch_without_duplicate_vectors():
    attempts = Counter()
    records = []

    async def handler(request):
        texts = json.loads(request.content)["input"]
        attempts[texts[0]] += 1
        if texts[0] == "0" and attempts["0"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {}})
        return response(texts)

    adapter = adapter_for(handler, records)
    try:
        result = await adapter.embed_batch([str(i) for i in range(5)], SPACE)
        assert result.vectors == [[float(i) + 1, 1.0] for i in range(5)]
        assert result.usage.total_tokens == 5
        assert attempts == {"0": 2, "2": 1, "4": 1}
        assert Counter(record.error for record in records) == {None: 3, "HTTP_429": 1}
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_empty_call_has_no_http_requests():
    records = []

    def handler(request):
        raise AssertionError("空输入不应发起 HTTP 请求")

    adapter = adapter_for(handler, records)
    try:
        result = await adapter.embed_batch([], SPACE)
        assert result.vectors == [] and result.usage.total_tokens is None
        assert adapter.calls == 0 and records == []
    finally:
        await adapter.close()
