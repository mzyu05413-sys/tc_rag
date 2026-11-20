"""测试用例生成问答助手：检索知识库事实后流式输出，不向终端暴露检索阶段。"""

import json
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any

from tc_rag.application.kb.search import SearchService
from tc_rag.application.ports.contracts import StreamingTextLLM
from tc_rag.application.ports.errors import ContextBudgetError
from tc_rag.application.services.chunking import TokenCounter, character_estimate
from tc_rag.domain.identity import fingerprint, new_id

# 没有可用证据时只说明检索事实，不代替需求文档推断业务规则。
NO_EVIDENCE = "知识库里没有检索到和这个问题相关的需求内容，请补充文档或换个问法。"


class AssistantService:
    def __init__(
        self, search: SearchService, model: StreamingTextLLM, prompt: str,
        context_chars: int = 12000,
        counter: TokenCounter = character_estimate, max_input_tokens: int = 12000,
        top_k: int = 5,
    ) -> None:
        if context_chars <= 0 or max_input_tokens <= 0 or top_k <= 0:
            raise ValueError("证据预算与候选数必须为正")
        self.search, self.model, self.prompt = search, model, prompt
        self.context_chars = context_chars
        self.counter, self.max_input_tokens = counter, max_input_tokens
        self.top_k = top_k

    async def stream(
        self, kb_id: str, question: str, *, snapshot_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        started = perf_counter()
        retrieval = await self.search.search(
            kb_id, question, top_k=self.top_k, snapshot_id=snapshot_id,
        )
        evidence: list[dict[str, Any]] = []
        remaining = self.context_chars
        for candidate in retrieval["candidates"]:
            if remaining <= 0:
                break
            text = candidate["text"][:remaining]
            evidence.append({"chunk_id": candidate["chunk_id"], "text": text,
                             "sources": candidate["sources"]})
            remaining -= len(text)
        run_id = new_id("assistant")
        request: dict[str, Any] = {}
        turns = list(history or [])
        while True:
            request = {"stage": "kb_assistant", "system": self.prompt,
                       "user": self.render(question, evidence), "history": turns}
            if not evidence or self.counter(json.dumps(request, ensure_ascii=False)) <= (
                self.max_input_tokens
            ):
                break
            evidence.pop()
        estimated = self.counter(json.dumps(request, ensure_ascii=False))
        base = {
            "run_id": run_id, "search_run_id": retrieval["run_id"], "kb_id": kb_id,
            "snapshot_id": retrieval["snapshot_id"], "query": question,
            "document_ids": retrieval["document_ids"], "evidence": evidence,
            "prompt_sha256": fingerprint(self.prompt),
            "runtime": self.search.components.runtime_identity,
            "estimated_input_tokens": estimated,
            "retrieved_candidates": len(retrieval["candidates"]),
            "sent_candidates": len(evidence),
        }
        repository = self.search.components.repository
        try:
            if estimated > self.max_input_tokens:
                raise ContextBudgetError(estimated, self.max_input_tokens)
            if not evidence:
                repository.save_result(run_id, {
                    **base, "status": "abstained", "answer_text": NO_EVIDENCE,
                    "total_seconds": perf_counter() - started,
                })
                yield NO_EVIDENCE
                return
            parts: list[str] = []
            async for delta in self.model.stream_text(request):
                parts.append(delta)
                yield delta
            repository.save_result(run_id, {
                **base, "status": "completed", "answer_text": "".join(parts),
                "total_seconds": perf_counter() - started,
            })
        except Exception as error:
            repository.save_result(run_id, {
                **base, "status": "error", "error_type": type(error).__name__,
                "total_seconds": perf_counter() - started,
            })
            raise

    @staticmethod
    def render(question: str, evidence: list[dict[str, Any]]) -> str:
        """按序号列出证据，模型引用序号即可回溯到对应片段。"""
        blocks = [{"index": number, "text": item["text"]}
                  for number, item in enumerate(evidence, 1)]
        return json.dumps({"question": question, "evidence": blocks}, ensure_ascii=False)
