"""诊断不进入旧调用 JSON；全流程读写用临时库与 MockTransport 验证。"""

import json
import sqlite3

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from tc_rag.bootstrap import build_pipeline
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.pipeline import CaseBatch, StageSnapshot
from tc_rag.infrastructure.providers import openai_sdk
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.interfaces import workflow


class LegacyInvocation(BaseModel):
    """旧版本契约，用于证明新增诊断不会让旧调用记录读取失败。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: str
    invocation_id: str
    run_id: str
    stage: str
    provider: str
    model: str
    usage: m.Usage
    timing: m.Timing
    error: str | None


@pytest.mark.asyncio
async def test_safe_diagnostics_saved_beside_legacy_compatible_invocations(
    tmp_path, monkeypatch
):
    calls = []

    def handler(request):
        calls.append(request)
        output = (
            {"cases": "private-value", "private-field": "private-body"}
            if len(calls) == 1 else {"cases": []}
        )
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(output),
            }}],
        })

    def client(**options):
        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ))

    monkeypatch.setattr(openai_sdk, "AsyncOpenAI", client)
    cfg = AppConfig.model_validate({
        "project": {"workdir": str(tmp_path / "work")},
        "storage": {"database": str(tmp_path / "fixture.sqlite")},
        "models": {"llm": {"model": "fixture", "base_url": "https://fixture.invalid/v1",
                            "api_key": "fake-offline-key"}},
    })
    pipeline = build_pipeline(cfg, "run", embedder=FakeEmbedder())
    pipeline.repository.save("runs", "run", m.Run(
        run_id="run", status="running", config_snapshot=cfg.model_dump(mode="json")
    ))
    events = []
    pipeline.llm.progress = events.append
    try:
        assert (await pipeline.llm.generate({"stage": "generate"}, CaseBatch)).output.cases == []
    finally:
        await pipeline.llm.close()
    saved = pipeline.repository.get("stage_snapshots", "run:invocations", StageSnapshot).payload
    assert len(saved["items"]) == 2
    first = saved["items"][0]
    paths = [detail["path"] for detail in saved["validation_errors"][first["invocation_id"]]]
    # 诊断顺序随 pydantic 版本与校验入口变化，只要求两类字段都被正确处理。
    assert ["cases"] in paths
    assert ["<unknown_field>"] in paths
    assert first["error"] == "SchemaOutputError"
    assert saved["items"][1]["error"] is None
    for item in saved["items"]:
        assert "validation_errors" not in item
        LegacyInvocation.model_validate(item)
    with sqlite3.connect(cfg.storage.database) as connection:
        rows = connection.execute("SELECT payload FROM model_invocations").fetchall()
    for (payload,) in rows:
        LegacyInvocation.model_validate_json(payload)
    repairs = [event for event in events if event.kind == "repair"]
    assert repairs and "cases: list_type" in repairs[0].validation_errors
    assert "private-" not in json.dumps(saved) + str(events)
    monkeypatch.setattr(workflow, "_config", lambda path: cfg)
    inspected = CliRunner().invoke(workflow.app, ["inspect", "run", "--stage", "invocations"])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["validation_errors"] == saved["validation_errors"]
