"""真实 SDK 配合内存 HTTP，验证截断、视觉限流与诊断；不发送网络请求。"""

import asyncio
import io
import json
import logging

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from tc_rag.application.ports.contracts import ParsedDocument, VisionResult
from tc_rag.application.ports.errors import OutputTruncatedError, VisionReadError
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.domain.models import DocumentBlock, Locator
from tc_rag.domain.pipeline import CaseBatch
from tc_rag.infrastructure.parsing.pdf import PdfParser
from tc_rag.infrastructure.providers.openai_sdk import OpenAIStructuredLLM
from tc_rag.infrastructure.providers.vision import OpenAICompatVision
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


def completion(content="图中有审批节点", *, truncated=False):
    return {
        "id": "offline",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length" if truncated else "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 8192 if truncated else 10,
            "total_tokens": 8292 if truncated else 110,
        },
    }


def client_for(handler):
    return AsyncOpenAI(
        api_key="offline-key",
        base_url="https://fixture.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
@pytest.mark.parametrize("limit", [None, 16384])
@pytest.mark.parametrize("truncated", [False, True])
async def test_output_limit_wire_format_and_truncation_usage(mode, limit, truncated):
    calls, records = [], []

    def handler(request):
        calls.append(json.loads(request.content))
        if mode == "chat_completions":
            return httpx.Response(200, json=completion('{"cases": []}', truncated=truncated))
        return httpx.Response(
            200,
            json={
                "id": "resp",
                "object": "response",
                "created_at": 0,
                "model": "fixture",
                "status": "incomplete" if truncated else "completed",
                "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
                "output": [
                    {
                        "id": "msg",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"cases": []}', "annotations": []}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 8192 if truncated else 10,
                    "total_tokens": 8292 if truncated else 110,
                },
            },
        )

    adapter = OpenAIStructuredLLM(
        Endpoint(model="fixture", api_mode=mode, max_output_tokens=limit),
        Runtime(),
        "run",
        records.append,
        client=client_for(handler),
    )
    try:
        if truncated:
            with pytest.raises(OutputTruncatedError):
                await adapter.generate({"stage": "extract"}, CaseBatch)
        else:
            assert (await adapter.generate({"stage": "extract"}, CaseBatch)).output.cases == []
    finally:
        await adapter.close()
    assert len(calls) == len(records) == 1
    field = "max_tokens" if mode == "chat_completions" else "max_output_tokens"
    if limit is None:
        assert field not in calls[0]
    else:
        assert calls[0][field] == limit
    assert records[0].usage.output_tokens == (8192 if truncated else 10)
    assert records[0].error == ("OutputTruncatedError" if truncated else None)


def picture():
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def vision_clock(monkeypatch, delays):
    """推进虚拟时钟，验证请求节奏而不让离线测试真实等待。"""
    now = [1000.0]

    async def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("tc_rag.infrastructure.providers.vision.asyncio.sleep", sleep)
    monkeypatch.setattr("tc_rag.infrastructure.providers.vision.perf_counter", lambda: now[0])
    return now


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503, 401])
async def test_vision_retries_only_transient_errors_and_records_every_attempt(monkeypatch, status):
    calls, records, events, delays, admissions = [], [], [], [], []

    vision_clock(monkeypatch, delays)

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                status,
                headers={"retry-after": "3"},
                json={"error": {"message": "private-provider-body"}},
            )
        return httpx.Response(200, json=completion())

    adapter = OpenAICompatVision(
        Endpoint(
            model="vision", supports_images=True, max_output_tokens=4096, thinking_mode="disabled"
        ),
        Runtime(max_attempts=3),
        "run",
        records.append,
        "解释可见信息",
        client_for(handler),
    )
    adapter.progress = events.append
    adapter.admit = lambda: admissions.append(True)
    try:
        if status == 401:
            with pytest.raises(VisionReadError, match="HTTP_401") as exc:
                await adapter.read(picture(), "图片")
            assert "private-provider-body" not in str(exc.value)
            assert len(calls) == 1 and delays == []
        else:
            assert (await adapter.read(picture(), "图片")).text == "图中有审批节点"
            assert len(calls) == 2 and delays == ([10] if status == 429 else [3])
            assert records[-1].error is None
        assert len(calls) == len(records) == len(admissions)
        assert records[0].error == f"HTTP_{status}"
        assert calls[0]["max_tokens"] == 4096
        assert calls[0]["thinking"] == {"type": "disabled"}
        assert sum(e.kind == "model_started" for e in events) == len(calls)
        assert sum(e.kind == "model_finished" for e in events) == len(calls)
        assert "private-provider-body" not in repr(events)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, None])
async def test_vision_retry_exhaustion_and_budget_are_bounded(monkeypatch, budget):
    vision_clock(monkeypatch, [])
    records = []
    adapter = OpenAICompatVision(
        Endpoint(model="vision", supports_images=True),
        Runtime(max_attempts=2, budget={"max_calls": budget}),
        "run",
        records.append,
        "图片",
        client_for(lambda _: httpx.Response(429, json={"error": {"message": "secret"}})),
    )
    try:
        with pytest.raises(VisionReadError) as exc:
            await adapter.read(picture(), "图片")
        assert exc.value.code == ("CALL_BUDGET" if budget else "HTTP_429")
        assert len(records) == (1 if budget else 2)
        assert all(r.error == "HTTP_429" for r in records)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_vision_cancellation_is_not_retried_or_swallowed():
    started, records = asyncio.Event(), []

    async def handler(_):
        started.set()
        await asyncio.Event().wait()

    adapter = OpenAICompatVision(
        Endpoint(model="vision", supports_images=True),
        Runtime(),
        "run",
        records.append,
        "图片",
        client_for(handler),
    )
    task = asyncio.create_task(adapter.read(picture(), "图片"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(records) == 1 and records[0].error == "CancelledError"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_vision_progress_summary_and_failed_images_do_not_become_evidence(tmp_path):
    class Reader:
        calls = 0

        async def read(self, image, context):
            self.calls += 1
            if image == b"bad":
                raise VisionReadError("HTTP_429")
            return VisionResult("图中有审批节点", "fixture", "vision/v1")

    store = LocalArtifactStore(tmp_path / "files")
    repo = SqliteRepository(tmp_path / "db")
    reader, events = Reader(), []
    service = VisionEvidenceService(repo, store, reader, {})
    service.progress = events.append
    blocks = [
        DocumentBlock(
            block_id=f"image_{i}",
            document_id="doc",
            kind="image",
            order=i,
                locator=Locator(page=1),
            content=store.put(f"{i}.png", image).model_dump_json(),
        )
        for i, image in enumerate([b"good", b"bad", b"good"])
    ]
    result, issues = await service.process(blocks)
    assert reader.calls == 2
    assert len([b for b in result if b.origin == "vision"]) == 2
    assert not any(b.block_id.startswith("image_1_") for b in result)
    assert any("HTTP_429" in issue.description for issue in issues)
    summary = events[-1]
    assert summary.kind == "vision_summary"
    assert (summary.total, summary.completed, summary.failed, summary.cached) == (3, 2, 1, 1)
    assert [e.completed for e in events if e.kind == "image_completed"] == [1, 2, 3]


@pytest.mark.parametrize("fail", [False, True])
def test_pdf_font_warning_summary_is_scoped_and_other_warnings_remain(
    tmp_path, monkeypatch, caplog, fail
):
    store = LocalArtifactStore(tmp_path)
    asset = store.put("fake.pdf", b"fixture")
    parser = PdfParser(store)
    logger = logging.getLogger("pdfminer.pdffont")
    previous = list(logger.filters)

    def parse(_):
        for _ in range(3):
            logger.warning(
                "Could not get FontBBox from font descriptor because %r cannot "
                "be parsed as 4 floats",
                None,
            )
        logger.warning("Other font warning")
        if fail:
            raise ValueError("fixture")
        return ParsedDocument(blocks=[])

    monkeypatch.setattr(parser, "_parse", parse)
    if fail:
        with pytest.raises(ValueError):
            parser.parse(asset)
    else:
        result = parser.parse(asset)
        assert len(result.issues) == 1 and "3次" in result.issues[0].description
    assert logger.filters == previous
    assert len([r for r in caplog.records if "FontBBox" in r.message]) == 1
    assert any("Other font warning" in r.message for r in caplog.records)
    logger.warning("Could not get FontBBox from font descriptor because None")
    assert len([r for r in caplog.records if "FontBBox" in r.message]) == 2
