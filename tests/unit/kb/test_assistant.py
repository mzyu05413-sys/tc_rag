"""问答助手只输出答案文本：顺序、证据序号和检索失败分支都由离线 Fake 固定。"""

import asyncio
import json
from typing import Any

import pytest

from tc_rag.application.kb.assistant import NO_EVIDENCE, AssistantService
from tc_rag.infrastructure.providers.fakes import FakeStreamingLLM


class _Repository:
    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}

    def save_result(self, run_id: str, result: dict[str, Any]) -> None:
        self.results[run_id] = result


class _Components:
    def __init__(self) -> None:
        self.repository = _Repository()
        self.runtime_identity = {"config": "test"}
        self.counter = len


class _Search:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.components = _Components()
        self.candidates = candidates
        self.queries: list[str] = []

    async def search(
        self, kb_id: str, query: str, *, top_k: int = 5,
        document_ids: set[str] | None = None, snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        self.queries.append(query)
        return {
            "run_id": "search_1", "kb_id": kb_id, "snapshot_id": snapshot_id or "s1",
            "document_ids": document_ids, "query": query,
            "candidates": self.candidates, "timings": {"total_seconds": 0.0},
        }


def collect(source: Any) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in source]

    return asyncio.run(run())


def test_stream_yields_model_deltas_in_order():
    search = _Search([{
        "chunk_id": "c1", "text": "会签需全员同意。",
        "sources": [{"document_id": "审批流.docx"}],
    }])
    model = FakeStreamingLLM([["会签", "需全员同意。"]])
    service = AssistantService(search, model, "系统提示")

    assert collect(service.stream("crm", "会签规则？")) == ["会签", "需全员同意。"]

    saved = list(search.components.repository.results.values())[0]
    assert saved["status"] == "completed"
    assert saved["answer_text"] == "会签需全员同意。"
    assert saved["sent_candidates"] == 1


def test_request_carries_numbered_evidence_without_stage_banners():
    search = _Search([{
        "chunk_id": "c1", "text": "会签需全员同意。", "sources": [{"document_id": "审批流.docx"}],
    }])
    model = FakeStreamingLLM([["好的。"]])
    collect(AssistantService(search, model, "系统提示").stream("crm", "会签规则？"))

    request = model.requests[0]
    assert request["stage"] == "kb_assistant"
    payload = json.loads(request["user"])
    assert payload["question"] == "会签规则？"
    assert payload["evidence"] == [{"index": 1, "text": "会签需全员同意。"}]


def test_no_candidate_returns_fixed_sentence_without_calling_model():
    search = _Search([])
    model = FakeStreamingLLM([["不应被使用"]])
    service = AssistantService(search, model, "系统提示")

    assert collect(service.stream("crm", "无覆盖的问题")) == [NO_EVIDENCE]
    assert model.requests == []
    saved = list(search.components.repository.results.values())[0]
    assert saved["status"] == "abstained"


def test_history_is_forwarded_to_the_model():
    search = _Search([{
        "chunk_id": "c1", "text": "原文", "sources": [],
    }])
    model = FakeStreamingLLM([["多轮答案"]])
    service = AssistantService(search, model, "系统提示")
    history = [
        {"role": "user", "content": "你是谁"},
        {"role": "assistant", "content": "我是测试用例助手"},
    ]

    assert collect(service.stream("crm", "那你能做什么", history=history)) == ["多轮答案"]
    assert model.requests[0]["history"] == history


def test_model_failure_is_recorded_and_reraised():
    search = _Search([{
        "chunk_id": "c1", "text": "原文", "sources": [],
    }])
    model = FakeStreamingLLM([RuntimeError("boom")])
    service = AssistantService(search, model, "系统提示")

    async def run() -> None:
        async for _ in service.stream("crm", "问题"):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    saved = list(search.components.repository.results.values())[0]
    assert saved["status"] == "error"
    assert saved["error_type"] == "RuntimeError"


def test_invalid_budget_is_rejected():
    with pytest.raises(ValueError):
        AssistantService(_Search([]), FakeStreamingLLM([]), "系统提示", top_k=0)
