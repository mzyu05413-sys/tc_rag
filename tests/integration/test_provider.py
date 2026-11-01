"""MockTransport 截断所有 HTTP；验证实际 SDK 的重试、修复、消耗和失败记录。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from tc_rag.core.config import Endpoint, Runtime
from tc_rag.domain.pipeline import CaseBatch, Extraction, PointBatch, case_batch_schema
from tc_rag.infrastructure.providers.openai_sdk import (
    OpenAIStructuredLLM,
    OutputTruncatedError,
    ProviderError,
)


def completion(content='{"cases": []}', reason="stop"):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }


def provider(
    handler,
    records,
    *,
    attempts=2,
    repairs=1,
    mode="chat_completions",
    max_calls=None,
    response_format="json_schema",
    thinking_mode="default",
):
    client = AsyncOpenAI(
        api_key="fake-offline-key",
        base_url="https://fixture.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIStructuredLLM(
        Endpoint(
            model="fixture",
            api_mode=mode,
            response_format=response_format,
            thinking_mode=thinking_mode,
        ),
        Runtime(max_attempts=attempts, budget={"max_calls": max_calls}),
        "run",
        records.append,
        client=client,
        repairs=repairs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_retry_and_usage(status):
    calls, records = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                status, headers={"retry-after": "0"}, json={"error": {"message": "private"}}
            )
        return httpx.Response(200, json=completion())

    adapter = provider(handler, records)
    try:
        result = await adapter.generate({"stage": "generate", "system": "生成 JSON"}, CaseBatch)
    finally:
        await adapter.close()
    assert len(calls) == len(records) == 2
    assert records[0].error == f"HTTP_{status}"
    assert records[0].usage.total_tokens is None
    assert result.usage.total_tokens == 13
    assert records[1].timing.duration_ms >= 0


@pytest.mark.asyncio
async def test_auth_fast_fail_does_not_expose_body():
    records = []
    adapter = provider(
        lambda r: httpx.Response(401, json={"error": {"message": "SECRET"}}), records
    )
    try:
        with pytest.raises(ProviderError, match="HTTP_401") as exc:
            await adapter.generate({"stage": "generate"}, CaseBatch)
        assert "SECRET" not in str(exc.value)
        assert len(records) == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_schema_repair_bounded_and_truncation_explicit():
    records, calls = [], []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json=completion("not json" if len(calls) == 1 else '{"cases": []}')
        )

    adapter = provider(handler, records)
    try:
        assert (await adapter.generate({"stage": "generate"}, CaseBatch)).output.cases == []
        assert len(records) == 2
        assert records[0].error == "SchemaOutputError"
    finally:
        await adapter.close()
    adapter = provider(lambda r: httpx.Response(200, json=completion("{", "length")), [])
    try:
        with pytest.raises(OutputTruncatedError):
            await adapter.generate({"stage": "generate"}, CaseBatch)
        assert adapter.calls == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_schema_repair_has_safe_field_details_without_echoing_invalid_content():
    calls, records = [], []

    def handler(request):
        body = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(body)
        content = (
            '{"cases": "private-value", "private-field": "private-body"}'
            if len(calls) == 1
            else '{"cases": []}'
        )
        return httpx.Response(200, json=completion(content))

    adapter = provider(handler, records)
    try:
        await adapter.generate({"stage": "generate"}, CaseBatch)
        details = calls[1]["validation_errors"]
        assert {"path": ["cases"], "error": "list_type", "expected_type": "array"} in details
        assert any(
            item["path"] == ["<unknown_field>"]
            and item["error"] == "extra_forbidden"
            and "cases" in item["allowed_fields"]
            for item in details
        )
        assert "private-" not in json.dumps(calls[1]) + str(records)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [True, False])
async def test_duplicate_extraction_ids_use_bounded_safe_repair(fixed):
    """同集合重复 ID 不再绕过修复，不把错误 ID 或正文发送到修复请求。"""
    calls, records, events = [], [], []
    duplicate = {
        "evidence": [
            {
                "evidence_id": "private-generated-id",
                "block_id": "b",
                "locator": {"page": 1},
                "quote": "private-output-text",
            }
        ],
        "features": [
            {
                "feature_id": "f_1",
                "name": "private-output-text",
                "description": "",
                "evidence_ids": ["private-generated-id"],
            }
        ],
    }
    duplicate["evidence"] *= 2
    valid = {
        "evidence": [
            {
                "evidence_id": "e_1",
                "block_id": "b",
                "locator": {"page": 1},
                "quote": "sample",
            }
        ],
        "features": [
            {
                "feature_id": "f_1",
                "name": "sample",
                "description": "",
                "evidence_ids": ["e_1"],
            }
        ],
    }

    def handler(request):
        calls.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        value = valid if fixed and len(calls) > 1 else duplicate
        return httpx.Response(200, json=completion(json.dumps(value)))

    adapter = provider(handler, records, attempts=1, repairs=3)
    adapter.progress = events.append
    try:
        if fixed:
            result = await adapter.generate({"stage": "extract"}, Extraction)
            assert result.output.features[0].evidence_ids == [result.output.evidence[0].evidence_id]
            assert len(calls) == 2 and records[-1].error is None
        else:
            with pytest.raises(ProviderError, match="unique ID within its collection") as exc:
                await adapter.generate({"stage": "extract"}, Extraction)
            assert "private-" not in str(exc.value)
            assert len(calls) == 4
        assert records[0].error == "SchemaOutputError"
        details = calls[1]["validation_errors"]
        assert any(
            "unique ID within its collection" in detail.get("constraint", "") for detail in details
        )
        assert "private-" not in json.dumps(calls[1]) + str(records)
        assert sum(event.kind == "repair" for event in events) == (1 if fixed else 3)
        assert not any(event.kind == "retry" for event in events)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_repaired_extraction_ids_continue_through_rag_to_joint_export(tmp_path):
    from test_pipeline import FixtureLLM, setup

    pipeline, document, _, _ = setup(tmp_path)
    fixture = FixtureLLM()
    calls, records = [], []

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        schema = {"extract": Extraction, "plan_points": PointBatch, "generate": CaseBatch}[
            payload["stage"]
        ]
        value = (await fixture.generate(payload, schema)).output.model_dump(mode="json")
        if payload["stage"] == "extract":
            # 跨类型复用局部编号可被命名空间确定性区分，不应触发修复。
            value["evidence"][0]["evidence_id"] = "1"
            value["features"][0]["feature_id"] = "1"
            value["features"][0]["evidence_ids"] = ["1"]
            value["rules"][0]["rule_id"] = "1"
            value["rules"][0]["feature_id"] = "1"
            value["rules"][0]["evidence_ids"] = ["1"]
        if len(calls) == 1:
            # 同类型出现两个相同 ID，必须修复后才可进入持久化和检索。
            value["evidence"].append(dict(value["evidence"][0]))
        return httpx.Response(200, json=completion(json.dumps(value)))

    adapter = provider(handler, records, attempts=1)
    pipeline.llm = adapter
    try:
        result = await pipeline.generate(document, "fixture", tmp_path / "out", run_id="run_demo")
        assert result["cases"] == 1
        assert [call["stage"] for call in calls] == [
            "extract",
            "extract",
            "plan_points",
            "generate",
        ]
        extracted = pipeline.read("run_demo", "extract")
        assert extracted["rules"][0]["feature_id"] == extracted["features"][0]["feature_id"]
        assert extracted["rules"][0]["evidence_ids"] == [extracted["evidence"][0]["evidence_id"]]
        assert records[0].error == "SchemaOutputError"
        assert all(record.error is None for record in records[1:])
        assert Path(result["xmind"]).is_file() and Path(result["excel"]).is_file()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_issue_enum_is_coerced_without_extra_call():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        content = json.dumps(
            {
                "issues": [
                    {
                        "issue_id": "i",
                        "kind": "invalid-kind",
                        "severity": "invalid-severity",
                        "description": "private-description",
                    }
                ]
            }
        )
        return httpx.Response(200, json=completion(content))

    adapter = provider(handler, [])
    try:
        result = await adapter.generate({"stage": "extract"}, Extraction)
        assert len(calls) == 1
        assert result.output.issues[0].kind == "other"
        assert result.output.issues[0].severity == "warning"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_extraction_repair_explains_enums_and_internal_mapping():
    calls = []

    def handler(request):
        calls.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        content = (
            json.dumps(
                {
                    "schema_version": "private-version",
                    "rule_mapping": {"private-rule-id": ["private-value"]},
                    "issues": [
                        {
                            "issue_id": "i",
                            "kind": "invalid-kind",
                            "severity": "warning",
                            "description": "private-description",
                        }
                    ],
                }
            )
            if len(calls) == 1
            else "{}"
        )
        return httpx.Response(200, json=completion(content))

    adapter = provider(handler, [])
    try:
        await adapter.generate({"stage": "extract"}, Extraction)
        repair = calls[1]
        assert "rule_mapping" in repair["repair"] and "{}" in repair["repair"]
        assert any(
            error["path"] == ["schema_version"] and error["allowed_values"] == ["1.0"]
            for error in repair["validation_errors"]
        )
        assert any(
            error["path"] == ["rule_mapping", "<unknown_field>"]
            and error["expected_type"] == "string"
            for error in repair["validation_errors"]
        )
        assert "private-" not in json.dumps(repair)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_budget_blocks_repair():
    records = []

    def timeout(request):
        raise httpx.ReadTimeout("private request", request=request)

    adapter = provider(timeout, records, attempts=1)
    try:
        with pytest.raises(ProviderError):
            await adapter.generate({}, CaseBatch)
        assert len(records) == 1 and records[0].usage.input_tokens is None
    finally:
        await adapter.close()
    adapter = provider(lambda r: httpx.Response(200, json=completion("bad")), [], max_calls=1)
    try:
        with pytest.raises(ProviderError, match="预算"):
            await adapter.generate({}, CaseBatch)
        assert adapter.calls == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["json_schema", "json_object"])
async def test_responses_mode_and_unknown_usage(response_format):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "fixture",
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
            },
        )

    adapter = provider(handler, [], mode="responses", response_format=response_format)
    try:
        result = await adapter.generate({"stage": "generate"}, CaseBatch)
        assert result.usage.total_tokens is None
        assert observed[0]["text"]["format"]["type"] == response_format
        assert observed[0]["store"] is False
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_json_object_sends_schema_and_rejects_invalid_structure():
    calls, records = [], []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        # 模拟 DeepSeek Chat：仅接受 JSON Object，不能携带 json_schema 格式。
        assert payload["response_format"] == {"type": "json_object"}
        body = json.loads(payload["messages"][1]["content"])
        assert body["output_schema"] == CaseBatch.model_json_schema()
        assert body["output_format"] == "JSON"
        assert body["stage"] == "generate"
        # 合法 JSON 不一定满足领域契约：错误字段类型仍必须进入有界修复。
        content = '{"cases": "invalid-type"}' if len(calls) == 1 else '{"cases": []}'
        return httpx.Response(200, json=completion(content))

    adapter = provider(handler, records, response_format="json_object")
    try:
        result = await adapter.generate({"stage": "generate", "system": "输出 JSON"}, CaseBatch)
        assert result.output.cases == []
        assert len(calls) == len(records) == 2
        assert records[0].error == "SchemaOutputError"
        assert "repair" in json.loads(calls[1]["messages"][1]["content"])
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_bad_request_is_not_retried_or_exposed():
    records = []
    adapter = provider(
        lambda r: httpx.Response(400, json={"error": {"message": "PRIVATE-REQUEST-BODY"}}),
        records,
        response_format="json_object",
    )
    try:
        with pytest.raises(ProviderError, match="HTTP_400") as error:
            await adapter.generate({"stage": "extract", "system": "输出 JSON"}, CaseBatch)
        assert "PRIVATE-REQUEST-BODY" not in str(error.value)
        assert len(records) == 1
        assert records[0].error == "HTTP_400"
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking_mode", ["default", "enabled", "disabled"])
async def test_thinking_switch_only_sent_when_explicit(thinking_mode):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    adapter = provider(handler, [], thinking_mode=thinking_mode)
    try:
        await adapter.generate({"stage": "extract"}, CaseBatch)
        if thinking_mode == "default":
            assert "thinking" not in observed[0]
        else:
            assert observed[0]["thinking"] == {"type": thinking_mode}
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_exceed_call_budget():
    entered, release = asyncio.Event(), asyncio.Event()
    requests, records = [], []

    async def handler(request):
        requests.append(request)
        if len(requests) == 2:
            entered.set()
        await release.wait()
        return httpx.Response(200, json=completion())

    adapter = provider(handler, records, max_calls=2)
    tasks = [
        asyncio.create_task(adapter.generate({"stage": "extract"}, CaseBatch)) for _ in range(5)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert len(requests) == len(records) == adapter.calls == 2
        assert sum(isinstance(result, ProviderError) for result in results) == 3
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
async def test_retry_repair_progress_balances_active_requests():
    events, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": "0"}, json={"error": "private"})
        return httpx.Response(200, json=completion("bad" if len(calls) == 2 else '{"cases": []}'))

    adapter = provider(handler, [])
    adapter.progress = events.append
    try:
        await adapter.generate({"stage": "extract"}, CaseBatch)
        assert [event.kind for event in events] == [
            "model_started",
            "model_finished",
            "retry",
            "model_started",
            "model_finished",
            "repair",
            "model_started",
            "model_finished",
        ]
        assert events[2].attempt == 1 and events[2].total == 2
        assert events[5].attempt == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("can_repair", [True, False])
async def test_single_point_case_limit_repairs_before_cache_and_bypasses_old_cache(
    tmp_path, can_repair
):
    """模型 HTTP 成功但单点超限：有界修复，不复用旧宽松 Schema 缓存。"""
    from tc_rag.bootstrap import build_pipeline
    from tc_rag.core.config import AppConfig
    from tc_rag.domain import models as m
    from tc_rag.domain.identity import fingerprint
    from tc_rag.domain.pipeline import StageSnapshot
    from tc_rag.infrastructure.providers.fakes import FakeEmbedder

    def output(count):
        return {"cases": [
            {
                "case_id": f"c{point}_{index}", "title": "校验测试目标",
                "point_ids": [f"p{point}"],
                "steps": [{"action": "执行校验", "expected": "观察结果",
                           "rule_ids": ["r"], "evidence_ids": ["e"]}],
            }
            for point, total in enumerate([1, 1, count, 2, 3]) for index in range(total)
        ]}

    calls, records, events = [], [], []

    def handler(request):
        body = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(body)
        assert body["limits"]["max_cases_per_point"] == 6
        return httpx.Response(200, json=completion(json.dumps(
            output(6 if can_repair and len(calls) > 1 else 8), ensure_ascii=False
        )))

    adapter = provider(handler, records, repairs=1, response_format="json_object")
    adapter.progress = events.append
    cfg = AppConfig.model_validate({
        "project": {"workdir": str(tmp_path / "work")},
        "storage": {"database": str(tmp_path / "data.sqlite")},
        "chunking": {"token_counter": "character_approx"},
        "models": {"embedding": {"model": "fixture"}},
        "retrieval": {"embedding_dimension": 3},
    })
    pipeline = build_pipeline(cfg, "run", adapter, FakeEmbedder())
    pipeline.active_run_id = "run"
    pipeline.repository.save(
        "runs", "run", m.Run(run_id="run", status="running", config_snapshot={})
    )
    old_request = {"stage": "generate"}
    old_schema = case_batch_schema([f"p{i}" for i in range(5)])
    model_config = cfg.models.llm.model_dump(mode="json")
    old_key = fingerprint([old_request, old_schema.model_json_schema(), model_config])
    old_id = f"run:cache:{old_key}"
    pipeline.repository.save("stage_snapshots", old_id, StageSnapshot(
        snapshot_id=old_id, run_id="run", stage="model_cache", payload={"output": output(8)}
    ))
    request = {**old_request, "limits": {"max_cases_per_point": 6}}
    schema = case_batch_schema([f"p{i}" for i in range(5)], 6)
    new_key = fingerprint([request, schema.model_json_schema(), model_config])
    new_id = f"run:cache:{new_key}"
    try:
        if can_repair:
            result = await pipeline._model(request, schema)
            assert len(result.output.cases) == 13
            assert pipeline.repository.get("stage_snapshots", new_id, StageSnapshot) is not None
            cached = await pipeline._model(request, schema)
            assert cached.output.model_dump() == result.output.model_dump()
            assert pipeline.cache_hits == 1
        else:
            with pytest.raises(ProviderError, match="Schema"):
                await pipeline._model(request, schema)
            assert pipeline.repository.get("stage_snapshots", new_id, StageSnapshot) is None
            assert pipeline.cache_hits == 0
        assert len(calls) == len(records) == 2
        assert records[0].error == "SchemaOutputError"
        assert calls[1]["validation_errors"][0]["constraint"].startswith("Each point_id")
        assert len([event for event in events if event.kind == "repair"]) == 1
        # 原缓存留作历史；没有修改生产数据或静默删除超限用例。
        original = pipeline.repository.get("stage_snapshots", old_id, StageSnapshot)
        assert original.payload["output"] == output(8)
    finally:
        await adapter.close()
