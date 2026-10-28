"""使用真实 SDK 和虚拟时钟检查视觉限流；不访问真实模型。"""

import asyncio
import io

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from tc_rag.application.ports.errors import VisionReadError
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.infrastructure.providers.vision import OpenAICompatVision


def image():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def success():
    return httpx.Response(
        200,
        json={
            "id": "fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "可见审批节点"},
                }
            ],
        },
    )


@pytest.fixture
def clock(monkeypatch):
    now, delays = [1000.0], []
    yield_once = asyncio.sleep

    async def sleep(seconds):
        delays.append(seconds)
        # 先让已排队的 SDK 任务启动，再推进时间，保持并发调度顺序。
        await yield_once(0)
        now[0] += seconds

    monkeypatch.setattr("tc_rag.infrastructure.providers.vision.perf_counter", lambda: now[0])
    monkeypatch.setattr("tc_rag.infrastructure.providers.vision.asyncio.sleep", sleep)
    return now, delays


def reader(handler, *, trusted=True, **runtime):
    records, events = [], []
    url = "https://open.bigmodel.cn/api/paas/v4" if trusted else "https://example.test/v1"
    client = AsyncOpenAI(
        api_key="fixture",
        base_url=url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = OpenAICompatVision(
        Endpoint(base_url=url, model="fixture", supports_images=True),
        Runtime(**runtime),
        "run",
        records.append,
        "解释图片",
        client,
    )
    adapter.progress = events.append
    return adapter, records, events


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["1302", "1305"])
async def test_cooldown_survives_exhausted_image_and_resets_after_success(clock, code):
    now, delays = clock
    starts = []

    def handler(_):
        starts.append(now[0])
        if len(starts) in {1, 2, 4}:
            return httpx.Response(429, json={"error": {"code": code, "message": "private-body"}})
        return success()

    adapter, records, events = reader(handler, max_attempts=2)
    try:
        with pytest.raises(VisionReadError, match=f"HTTP_429_BIGMODEL_{code}"):
            await adapter.read(image(), "第一张")
        await adapter.read(image(), "第二张")
        await adapter.read(image(), "第三张")
        assert starts == [1000, 1010, 1030, 1030, 1040]
        assert delays == [10, 20, 10]
        assert len(records) == 5
        assert any(e.kind == "request_wait" and e.duration_ms == 20000 for e in events)
        assert "private-body" not in repr(records) + repr(events)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_continuous_rate_limits_have_bounded_cooldown(clock):
    adapter, records, _ = reader(
        lambda _: httpx.Response(429, json={"error": {"code": "1302"}}),
        max_attempts=1,
    )
    try:
        for _ in range(6):
            with pytest.raises(VisionReadError):
                await adapter.read(image(), "图片")
        assert clock[1] == [10, 20, 40, 60, 60]
        assert len(records) == 6
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["1113", "1308", "1310", "1311", "1313", "1315", "1321"])
async def test_known_account_errors_do_not_retry_or_request_remaining_images(clock, code):
    adapter, records, events = reader(
        lambda _: httpx.Response(429, json={"error": {"code": code, "message": "private-body"}}),
    )
    try:
        for _ in range(3):
            with pytest.raises(VisionReadError, match=f"HTTP_429_BIGMODEL_{code}"):
                await adapter.read(image(), "图片")
        assert len(records) == 1
        assert clock[1] == []
        assert sum(e.kind == "vision_blocked" for e in events) == 1
        assert not any(e.kind == "retry" for e in events)
        assert "private-body" not in repr(records) + repr(events)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trusted,code", [(False, "1113"), (True, "private-secret"), (True, {"x": 1})]
)
async def test_foreign_or_invalid_business_codes_use_generic_error(clock, trusted, code):
    adapter, records, events = reader(
        lambda _: httpx.Response(429, json={"error": {"code": code, "message": "private-body"}}),
        trusted=trusted,
        max_attempts=2,
    )
    try:
        with pytest.raises(VisionReadError) as exc:
            await adapter.read(image(), "图片")
        assert exc.value.code == "HTTP_429"
        assert len(records) == 2
        assert not any(e.kind == "vision_blocked" for e in events)
        assert "private-secret" not in repr(records) + repr(events)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_minimum_interval_applies_to_actual_concurrent_requests(clock, monkeypatch):
    starts = []
    sent = asyncio.Queue()

    def handler(_):
        starts.append(clock[0][0])
        sent.put_nowait(True)
        return success()

    async def sleep(seconds):
        # SDK 自身也会异步排队；只有上一请求到达模拟网络，才推进虚拟时间。
        await asyncio.wait_for(sent.get(), 2)
        clock[1].append(seconds)
        clock[0][0] += seconds

    monkeypatch.setattr("tc_rag.infrastructure.providers.vision.asyncio.sleep", sleep)

    adapter, records, _ = reader(handler, vision_min_interval_seconds=3)
    try:
        await asyncio.gather(*(adapter.read(image(), "图片") for _ in range(3)))
        assert starts == [1000, 1003, 1006]
        assert len(records) == 3 and clock[1] == [3, 3]
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_waiting_concurrent_requests_recheck_budget_before_sending(clock):
    adapter, records, _ = reader(
        lambda _: success(),
        vision_min_interval_seconds=3,
        budget={"max_calls": 1},
    )
    try:
        results = await asyncio.gather(
            *(adapter.read(image(), "图片") for _ in range(3)),
            return_exceptions=True,
        )
        assert len(records) == adapter.calls == 1
        errors = [result for result in results if isinstance(result, VisionReadError)]
        assert len(errors) == 2 and all(error.code == "CALL_BUDGET" for error in errors)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_cancellation_during_cooldown_does_not_record_a_fake_request(monkeypatch):
    waiting = asyncio.Event()
    adapter, records, events = reader(lambda _: success(), vision_min_interval_seconds=60)

    async def sleep(_):
        waiting.set()
        await asyncio.Event().wait()

    try:
        await adapter.read(image(), "第一张")
        monkeypatch.setattr("tc_rag.infrastructure.providers.vision.asyncio.sleep", sleep)
        task = asyncio.create_task(adapter.read(image(), "第二张"))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.calls == len(records) == 1
        assert sum(e.kind == "model_started" for e in events) == 1
    finally:
        await adapter.close()
