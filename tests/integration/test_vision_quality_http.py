"""真实 SDK + HTTP 模拟验证多视图、复核、预算和原始结果保存。"""

import io
import json

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from tc_rag.application.ports.errors import VisionReadError
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.core.config import AppConfig, Endpoint, Runtime, VisionProcessing
from tc_rag.domain.models import DocumentBlock, Locator, Run
from tc_rag.domain.rag import RagRecord
from tc_rag.infrastructure.providers.vision import OpenAICompatVision
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.vision_check import execute


def response(text):
    return httpx.Response(200, json={
        "id": "fixture", "created": 0, "model": "fixture", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": text,
        }}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })


def payload(text="0/300", view="overview"):
    return json.dumps({"image_type": "ui", "facts": [{
        "fact_id": "f1", "kind": "text", "text": text, "view_ids": [view],
    }]})


def adapter(outputs, *, options=None, budget=None):
    requests, records = [], []

    def handle(request):
        requests.append(json.loads(request.content))
        return response(outputs[min(len(requests) - 1, len(outputs) - 1)])

    client = AsyncOpenAI(
        api_key="fixture", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    reader = OpenAICompatVision(
        Endpoint(model="fixture", supports_images=True), Runtime(budget=budget or {}),
        "fixture-run", records.append, "初次识别", client,
        processing=options or VisionProcessing(), review_prompt="重新查看图片",
    )
    return reader, requests, records


def image(size=(400, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_overview_tiles_and_same_context_are_rechecked_with_two_audited_calls():
    reader, requests, records = adapter([payload("0/800"), payload()])
    try:
        result = await reader.read(
            image((600, 4200)), '{"neighbors": [], "heading_path": ["评论"]}'
        )
        assert len(requests) == len(records) == 2
        assert "0/300" in result.text and "0/800" not in result.text
        assert result.details["model_verified"] and not result.details["human_verified"]
        assert "0/800" in result.details["draft"]
        first = requests[0]["messages"][1]["content"]
        second = requests[1]["messages"][1]["content"]
        images = [item for item in first if item["type"] == "image_url"]
        assert len(images) > 1 and all(i["image_url"]["detail"] == "high" for i in images)
        assert images == [item for item in second if item["type"] == "image_url"]
        assert result.version == "vision/v2"
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_invalid_draft_is_rebuilt_but_invalid_final_is_not_accepted():
    reader, requests, _ = adapter(["broken JSON", payload()])
    try:
        result = await reader.read(image(), "章节")
        assert not result.details["draft_valid"] and len(requests) == 2
    finally:
        await reader.close()
    reader, requests, _ = adapter([payload(), payload(view="unknown")])
    try:
        with pytest.raises(VisionReadError, match="INVALID_VISUAL_STRUCTURE"):
            await reader.read(image(), "章节")
        assert len(requests) == 2
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_verification_uses_same_call_budget_and_never_claims_unfinished_draft_as_result():
    reader, requests, _ = adapter([payload()], budget={"max_calls": 1})
    try:
        with pytest.raises(VisionReadError, match="CALL_BUDGET"):
            await reader.read(image(), "章节")
        assert len(requests) == 1
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_context_change_invalidates_cache_and_preserves_structured_result(tmp_path):
    repository = SqliteRepository(tmp_path / "data.sqlite")
    store = LocalArtifactStore(tmp_path / "assets")
    asset = store.put("image.png", image())
    photo = DocumentBlock(
        block_id="img", document_id="d", order=0, kind="image", locator=Locator(),
        content=asset.model_dump_json(),
    )
    paragraph = DocumentBlock(
        block_id="p", document_id="d", order=1, kind="paragraph", locator=Locator(),
        content="明确说明：不得提交空白评论。",
    )
    reader, requests, _ = adapter([payload()])
    service = VisionEvidenceService(
        repository, store, reader, {"version": "v2"}, context_blocks=2
    )
    try:
        first, _ = await service.process([photo, paragraph])
        again, _ = await service.process([photo, paragraph])
        assert first == again and len(requests) == 2
        key = first[1].block_id[len(photo.block_id) + 1:]
        cached = repository.get("rag_records", key, RagRecord)
        assert cached.payload["details"]["description"]["facts"][0]["text"] == "0/300"
        changed = paragraph.model_copy(update={"content": "产品说明更新。"})
        await service.process([photo, changed])
        assert len(requests) == 4
        service.identity = {"version": "v2-revised"}
        await service.process([photo, changed])
        assert len(requests) == 6
        assert first[1].origin == "vision" and first[1].locator == photo.locator
    finally:
        await reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_final", [True, False])
async def test_isolated_verification_uses_only_vision_and_separate_database(
    tmp_path, monkeypatch, capsys, valid_final
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(payload(view="unknown" if not valid_final and len(requests) == 2
                                else "overview"))

    client = AsyncOpenAI(
        api_key="fixture", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "tc_rag.infrastructure.providers.vision.AsyncOpenAI", lambda **kwargs: client
    )
    store = LocalArtifactStore(tmp_path / "assets")
    asset = store.put("image.png", image())
    photo = DocumentBlock(
        block_id="img", document_id="d", order=0, kind="image", locator=Locator(),
        content=asset.model_dump_json(),
    )
    config = AppConfig.model_validate({
        "project": {"workdir": str(store.root)},
        "runtime": {"cache_enabled": False},
        "models": {"vision": {
            "model": "fixture", "supports_images": True, "api_key": "fixture",
        }},
    })
    assert not config.models.llm.model and not config.models.embedding.model
    rows = [{"block_id": "img", "result": None}]
    directory = tmp_path / "verification"
    directory.mkdir()
    records = await execute(config, [photo], rows, directory, "vision_test")
    assert len(records) == len(requests) == 2
    assert all(record.stage == "vision" for record in records)
    output = capsys.readouterr().err
    if valid_final:
        assert rows[0]["result"]["origin"] == "vision"
        assert rows[0]["structured"]["model_verified"]
        assert "流程完成" in output and "流程失败" not in output
    else:
        assert rows[0]["result"] is None and rows[0]["status"] == "failed"
        assert "INVALID_VISUAL_STRUCTURE" in rows[0]["issues"][0]["description"]
        assert "流程失败" in output and "流程完成" not in output
        assert "[失败] 01/1 图片解释" in output
        saved = SqliteRepository(directory / "vision.sqlite", read_only=True)
        assert saved.get("runs", "vision_test", Run).status == "partial_failed"
    assert rows[0]["request_stats"]["requests"] == 2
    assert rows[0]["request_stats"]["input_tokens"] == 6
    assert rows[0]["request_stats"]["output_tokens"] == 4
    assert (directory / "vision.sqlite").exists()
