"""增量视觉策略均使用真实 SDK 与 HTTP 模拟；不访问模型服务。"""

import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from test_vision_quality_http import image, payload, response

from tc_rag.application.services.design import drop_resolved_image_issues, issue
from tc_rag.application.services.quality import inspect_quality
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.bootstrap import build_vision_service, load_prompts
from tc_rag.core.config import AppConfig, Endpoint, Runtime, VisionProcessing, load_config
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import DocumentBlock, Locator
from tc_rag.infrastructure.providers.vision import OpenAICompatVision, extract_json_object
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


def make_reader(outputs, options=None, *, cross=False, budget=None, supports_json=False):
    requests, records, admissions = [], [], []

    def handler(request):
        requests.append((request.url.host, json.loads(request.content)))
        result = outputs[min(len(requests) - 1, len(outputs) - 1)]
        return result if isinstance(result, httpx.Response) else response(result)

    def client(host):
        return AsyncOpenAI(
            api_key="fixture", base_url=f"https://{host}/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    main = Endpoint(model="main", supports_images=True, supports_json_object=supports_json)
    reader = OpenAICompatVision(
        main, Runtime(max_attempts=1, budget=budget or {}), "run", records.append,
        "识别", client("main.test"), processing=options or VisionProcessing(),
        review_prompt="复核",
        review_endpoint=Endpoint(model="review", supports_images=True) if cross else None,
        review_client=client("review.test") if cross else None,
    )
    reader.admit = lambda: admissions.append(True)
    return reader, requests, records, admissions


@pytest.mark.parametrize("wrapped", [
    lambda s: s, lambda s: f"```json\n{s}\n```", lambda s: f"说明\n{s}\n结束",
])
def test_json_wrappers_and_braces_in_strings(wrapped):
    text = payload("条件包含 } 和 {，不得丢失")
    assert json.loads(extract_json_object(wrapped(text))) == json.loads(text)


@pytest.mark.parametrize("raw", ["", "{}{}", '{"facts":[{}]', '说明 {"a":"}'] )
def test_json_truncation_empty_and_multiple_objects_are_rejected(raw):
    with pytest.raises(ValueError):
        extract_json_object(raw)


@pytest.mark.asyncio
async def test_wrapped_draft_is_valid_without_review_and_original_draft_is_preserved():
    raw = "```json\n" + payload() + "\n```"
    reader, requests, _, _ = make_reader([raw], VisionProcessing(verify=False, detail=None))
    try:
        result = await reader.read(image(), "章节")
        assert result.details["draft"] == raw and result.details["draft_valid"]
        assert len(requests) == 1
        assert "response_format" not in requests[0][1]
        for part in requests[0][1]["messages"][1]["content"]:
            if part["type"] == "image_url":
                assert "detail" not in part["image_url"]
    finally:
        await reader.close()


@pytest.mark.parametrize("strategy,retained", [("fail", False), ("keep_draft", True)])
@pytest.mark.parametrize("failure", ["bad JSON", httpx.Response(400, json={"error": "fixture"})])
@pytest.mark.asyncio
async def test_review_failure_policy_with_same_or_cross_model(strategy, retained, failure):
    reader, requests, records, _ = make_reader(
        [payload(), failure], VisionProcessing(verify_failure=strategy), cross=True
    )
    try:
        if retained:
            result = await reader.read(image(), "章节")
            assert "未复核" in result.text and not result.details["model_verified"]
            assert result.details["verify_error"] in {"HTTP_400", "INVALID_VISUAL_STRUCTURE"}
            assert result.model == "main" and result.details["review_model"] == "review"
        else:
            with pytest.raises(RuntimeError):
                await reader.read(image(), "章节")
        assert len(requests) == 2 and records[1].stage == "vision_review"
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_invalid_draft_cannot_be_retained_and_config_error_is_not_downgraded():
    reader, _, _, _ = make_reader(
        ["bad JSON"], VisionProcessing(verify_failure="keep_draft")
    )
    try:
        with pytest.raises(RuntimeError, match="INVALID_VISUAL_STRUCTURE"):
            await reader.read(image(), "章节")
    finally:
        await reader.close()
    reader, _, _, _ = make_reader(
        [payload()], VisionProcessing(verify_failure="keep_draft"), cross=True
    )

    async def invalid_config(*args):
        raise ValueError("未配置价格表")

    reader.reviewer._request = invalid_config
    try:
        with pytest.raises(ValueError, match="未配置价格表"):
            await reader.read(image(), "章节")
    finally:
        await reader.close()


@pytest.mark.parametrize("cross", [False, True])
@pytest.mark.asyncio
async def test_keep_draft_respects_shared_budget(cross):
    reader, requests, records, admissions = make_reader(
        [payload()], VisionProcessing(verify_failure="keep_draft"),
        cross=cross, budget={"max_calls": 1},
    )
    try:
        result = await reader.read(image(), "章节")
        assert result.details["verify_error"] == "CALL_BUDGET"
        assert len(requests) == len(records) == len(admissions) == 1
    finally:
        await reader.close()


@pytest.mark.parametrize("case,expected", [
    ("simple", 1), ("invalid", 2), ("flow", 2), ("tiles", 2),
    ("uncertain", 2), ("many_facts", 2), ("at_threshold", 1),
])
@pytest.mark.asyncio
async def test_auto_review_deterministic_conditions(case, expected):
    draft = json.loads(payload())
    if case == "flow":
        draft["image_type"] = "flowchart"
    if case == "uncertain":
        draft["uncertainties"] = [{"text": "连线不明确", "view_ids": ["overview"]}]
    if case == "many_facts":
        draft["facts"].append({**draft["facts"][0], "fact_id": "f2"})
    raw = "invalid" if case == "invalid" else json.dumps(draft)
    reader, requests, _, _ = make_reader(
        [raw, payload()], VisionProcessing(verify="auto", auto_fact_threshold=1)
    )
    try:
        result = await reader.read(image((600, 4200) if case == "tiles" else (400, 200)), "章节")
        assert len(requests) == expected and result.details["model_verified"] == (expected == 2)
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_cross_model_uses_second_endpoint_json_capability_and_total_admit():
    reader, requests, records, admissions = make_reader(
        [payload("草稿"), payload("修订")], cross=True, supports_json=True
    )
    try:
        result = await reader.read(image(), "章节")
        assert [host for host, _ in requests] == ["main.test", "review.test"]
        assert requests[0][1]["response_format"] == {"type": "json_object"}
        assert "response_format" not in requests[1][1]
        assert [record.stage for record in records] == ["vision", "vision_review"]
        assert len(admissions) == 2 and result.model == "review" and "修订" in result.text
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_skipped_image_has_no_budget_failure_or_unresolved_placeholder(tmp_path):
    reader, requests, _, _ = make_reader([payload()])
    store = LocalArtifactStore(tmp_path / "assets")
    asset = store.put("tiny.png", image((16, 12)))
    photo = DocumentBlock(block_id="img", document_id="doc", order=0, kind="image",
                          locator=Locator(), content=asset.model_dump_json())
    service = VisionEvidenceService(SqliteRepository(tmp_path / "data.sqlite"), store, reader, {})
    events = []
    service.progress = events.append
    try:
        blocks, issues = await service.process([photo])
        assert blocks == [photo] and not requests and not reader.calls
        assert issues[0].severity == "info" and issues[0].description == "图片过小，已跳过"
        assert events[-1].skipped == 1 and events[-1].failed == events[-1].completed == 0
        unresolved = issue("unparsed", "图片未解析：" + asset.asset_id, [asset.asset_id])
        assert drop_resolved_image_issues([unresolved, *issues], blocks) == issues
        report = inspect_quality(
            {"config_snapshot": {"parsing": {"vision_enabled": True}}},
            {"chunk": {"blocks": [photo.model_dump()], "chunks": [],
                       "issues": [item.model_dump() for item in issues]}},
        )
        pairing = next(check for check in report["checks"] if check["check_id"] == "Q-PA-01")
        assert pairing["status"] == "pass" and pairing["counts"]["skipped"] == 1
    finally:
        await reader.close()


def test_default_snapshots_exactly_match_pre_increment_baseline_and_env_role(tmp_path):
    baseline = json.loads((Path(__file__).parents[1] / "fixtures" /
                           "vision_defaults_before_increment.json").read_text(encoding="utf-8"))
    assert AppConfig().model_dump(mode="json") == baseline
    assert "min_edge" not in VisionProcessing().model_dump()
    cfg = load_config(environ={"TC_RAG_VISION_REVIEW_MODEL": "review"})
    assert cfg.models.vision_review.model == "review" and not cfg.models.vision_review.enabled
    cfg.models.vision_review.enabled = True
    cfg.models.vision_review.api_key = SecretStr("secret-sentinel")
    assert "secret-sentinel" not in cfg.model_dump_json()


@pytest.mark.asyncio
async def test_default_cache_identity_is_unchanged_and_review_is_only_added_when_enabled(tmp_path):
    cfg = AppConfig.model_validate({"models": {"vision": {
        "model": "main", "supports_images": True, "api_key": "fixture"
    }}})
    prompts, _ = load_prompts(Path("prompts"))
    service = build_vision_service(
        cfg, SqliteRepository(tmp_path / "data.sqlite"), LocalArtifactStore(tmp_path / "assets"),
        "run", lambda record: None, prompts,
    )
    try:
        assert service.identity == {
            "endpoint": "", "model": "main", "prompt_version": "vision/v2",
            "prompt_fingerprint": fingerprint(prompts["vision"]),
            "review_prompt_fingerprint": fingerprint(prompts["vision_verify"]),
            "preprocessing": {"version": "v2", "format": "PNG", **{
                "max_edge": 2048, "tile_edge": 1536, "tile_overlap": 192, "max_tiles": 16,
                "detail": "high", "context_blocks": 2, "context_chars": 6000, "verify": True,
            }},
        }
    finally:
        await service.reader.close()
    cfg.models.vision_review = Endpoint(
        enabled=True, model="review", supports_images=True, base_url="https://review.test/v1",
        api_key="secret-sentinel",
    )
    other = build_vision_service(
        cfg, service.repository, service.store, "run", lambda r: None, prompts
    )
    try:
        assert other.identity["review_endpoint"]["model"] == "review"
        assert "secret-sentinel" not in json.dumps(other.identity)
        assert "secret-sentinel" not in cfg.model_dump_json()
    finally:
        await other.reader.close()
    cfg.models.vision_review = cfg.models.vision.model_copy()
    with pytest.raises(ValueError, match="不能是相同"):
        build_vision_service(cfg, service.repository, service.store, "run", lambda r: None, prompts)
