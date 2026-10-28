"""仅用虚构密钥和模拟 HTTP 验证配置凭据，不访问真实服务。"""

import importlib
import io
import json

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image
from typer.testing import CliRunner

from tc_rag.application.ports.contracts import EmbeddingSpace, RankedCandidate
from tc_rag.core.config import Endpoint, Runtime
from tc_rag.domain.pipeline import CaseBatch
from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder
from tc_rag.infrastructure.providers.openai_sdk import OpenAIStructuredLLM
from tc_rag.infrastructure.providers.rerank import HttpReranker
from tc_rag.infrastructure.providers.vision import OpenAICompatVision
from tc_rag.interfaces import workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["llm", "embedding", "vision", "rerank"])
async def test_adapter_sends_file_key_only_in_authorization(monkeypatch, role):
    secret = "fixture-local-key-not-for-export"
    monkeypatch.setenv("FIXTURE_OLD_KEY", "fixture-stale-environment-key")
    endpoint = Endpoint(
        api_key=secret,
        api_key_env="FIXTURE_OLD_KEY",
        model="fixture",
        base_url="https://fixture.invalid/v1/rerank"
        if role == "rerank"
        else "https://fixture.invalid/v1",
        adapter="http" if role == "rerank" else "openai_sdk",
        supports_images=True,
    )
    calls, records = [], []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {secret}"
        assert secret.encode() not in request.content
        if role == "embedding":
            body = {
                "model": "fixture",
                "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        elif role == "rerank":
            body = {"results": [{"index": 0, "relevance_score": 0.8}]}
        else:
            body = {
                "id": "fixture-response",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"cases": []}' if role == "llm" else "图片中的审批节点",
                        },
                    }
                ],
            }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    if role == "rerank":
        # 保留真实 HTTP 客户端，仅替换网络传输；仍由生产构造函数取得凭据。
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            "tc_rag.infrastructure.providers.rerank.httpx.AsyncClient",
            lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        adapter = HttpReranker(endpoint, Runtime(), "run", records.append)
    else:
        module_name = {"llm": "openai_sdk", "embedding": "embedding", "vision": "vision"}[role]
        module = importlib.import_module(f"tc_rag.infrastructure.providers.{module_name}")

        def sdk_client(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport))

        monkeypatch.setattr(module, "AsyncOpenAI", sdk_client)
        if role == "llm":
            adapter = OpenAIStructuredLLM(endpoint, Runtime(), "run", records.append)
        elif role == "embedding":
            adapter = OpenAIEmbedder(endpoint, Runtime(), "run", records.append)
        else:
            adapter = OpenAICompatVision(
                endpoint, Runtime(), "run", records.append, "仅解释可见内容"
            )

    try:
        if role == "llm":
            result = await adapter.generate({"stage": "generate"}, CaseBatch)
            assert result.output.cases == []
        elif role == "embedding":
            result = await adapter.embed_batch(
                ["测试文本"], EmbeddingSpace("fixture", "fixture", 2)
            )
            assert len(result.vectors) == 1
        elif role == "rerank":
            result = await adapter.rank("查询", [RankedCandidate("e1", "测试文本")])
            assert result[0].object_id == "e1"
        else:
            buffer = io.BytesIO()
            Image.new("RGB", (10, 10), "white").save(buffer, format="PNG")
            result = await adapter.read(buffer.getvalue(), "需求图片")
            assert result.text == "图片中的审批节点"
    finally:
        await adapter.close()
    assert len(calls) == len(records) == 1
    assert records[0].error is None
    assert secret not in records[0].model_dump_json()
    assert secret not in repr(endpoint)


def test_doctor_accepts_all_file_keys_without_environment(tmp_path, monkeypatch):
    secret = "fixture-doctor-file-key"
    roles = ["llm", "embedding", "vision", "rerank"]
    for role in roles:
        monkeypatch.delenv(f"TC_RAG_{role.upper()}_API_KEY", raising=False)
    config = tmp_path / "local.yaml"
    data = {
        "models": {
            role: {
                "api_key": secret,
                "model": "fixture",
                "enabled": True,
                "supports_images": True,
                "adapter": "http" if role == "rerank" else "openai_sdk",
                "base_url": "https://fixture.invalid/rerank",
            }
            for role in roles
        },
        "parsing": {"vision_enabled": True},
    }
    config.write_text(json.dumps(data), encoding="utf-8")

    def no_pipeline(*args, **kwargs):
        pytest.fail("离线诊断不得创建网络客户端")

    monkeypatch.setattr(workflow, "build_pipeline", no_pipeline)
    result = CliRunner().invoke(workflow.app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "未调用模型" in result.output
    assert secret not in result.output
    assert list(tmp_path.iterdir()) == [config]
