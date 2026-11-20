"""流式适配器用桩客户端验证：逐段返回文本、不发送结构化输出参数、失败落调用记录。"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tc_rag.core.config import Endpoint, Runtime
from tc_rag.infrastructure.providers.openai_sdk import OpenAIStructuredLLM, ProviderError


class _Stream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _Completions:
    def __init__(self, chunks: list[Any] | None = None, error: Exception | None = None) -> None:
        self.chunks, self.error, self.kwargs = chunks or [], error, {}

    async def create(self, **kwargs: Any) -> _Stream:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return _Stream(self.chunks)


def _chunk(content: str | None, usage: Any = None) -> Any:
    choices = [] if content is None else [
        SimpleNamespace(delta=SimpleNamespace(content=content)),
    ]
    return SimpleNamespace(choices=choices, usage=usage)


def _build(completions: _Completions, endpoint: Endpoint | None = None) -> tuple[
    OpenAIStructuredLLM, list[Any]
]:
    invocations: list[Any] = []
    llm = OpenAIStructuredLLM(
        endpoint or Endpoint(model="stub", enabled=True), Runtime(), "run",
        invocations.append,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return llm, invocations


def _collect(source: Any) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in source]

    return asyncio.run(run())


def test_stream_text_yields_deltas_without_structured_output_options():
    completions = _Completions([_chunk("会签"), _chunk(None), _chunk("需全员同意。")])
    llm, invocations = _build(completions)

    assert _collect(llm.stream_text({
        "stage": "kb_assistant", "system": "系统", "user": "问题",
    })) == ["会签", "需全员同意。"]

    assert completions.kwargs["stream"] is True
    assert "response_format" not in completions.kwargs
    assert completions.kwargs["messages"][0] == {"role": "system", "content": "系统"}
    assert len(invocations) == 1
    assert invocations[0].stage == "kb_assistant"
    assert invocations[0].error is None


def test_stream_text_places_history_between_system_and_user():
    completions = _Completions([_chunk("好")])
    llm, _ = _build(completions)
    _collect(llm.stream_text({
        "stage": "kb_assistant", "system": "系统", "user": "新问题",
        "history": [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧答案"},
        ],
    }))
    assert completions.kwargs["messages"] == [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧答案"},
        {"role": "user", "content": "新问题"},
    ]


def test_stream_text_keeps_usage_unknown_when_provider_omits_it():
    llm, invocations = _build(_Completions([_chunk("答案")]))
    _collect(llm.stream_text({"stage": "kb_assistant", "system": "s", "user": "u"}))
    assert invocations[0].usage.total_tokens is None


def test_stream_text_records_usage_returned_by_provider():
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
    llm, invocations = _build(_Completions([_chunk("答案", usage=usage)]))
    _collect(llm.stream_text({"stage": "kb_assistant", "system": "s", "user": "u"}))
    assert invocations[0].usage.total_tokens == 12


def test_stream_text_wraps_provider_failure_and_records_error():
    llm, invocations = _build(_Completions(error=RuntimeError("boom")))
    with pytest.raises(ProviderError):
        _collect(llm.stream_text({"stage": "kb_assistant", "system": "s", "user": "u"}))
    assert invocations[0].error == "RuntimeError"


def test_stream_text_rejects_responses_api_mode():
    llm, invocations = _build(
        _Completions([_chunk("答案")]),
        Endpoint(model="stub", enabled=True, api_mode="responses"),
    )
    with pytest.raises(ProviderError):
        _collect(llm.stream_text({"stage": "kb_assistant", "system": "s", "user": "u"}))
    assert invocations == []
