"""检索后引用问答，证据预算和引文结构检查独立于语义人工审核。"""

import json
from time import perf_counter
from typing import Any

from tc_rag.application.kb.search import SearchService
from tc_rag.application.ports.contracts import StructuredLLM
from tc_rag.application.ports.errors import ContextBudgetError
from tc_rag.application.progress import ProgressEvent, report
from tc_rag.application.services.chunking import TokenCounter, character_estimate
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.kb.answer import KnowledgeAnswer


class AnswerService:
    def __init__(
        self, search: SearchService, model: StructuredLLM, prompt: str,
        context_chars: int = 12000,
        counter: TokenCounter = character_estimate, max_input_tokens: int = 12000,
    ) -> None:
        if context_chars <= 0 or max_input_tokens <= 0:
            raise ValueError("证据预算必须为正")
        self.search, self.model, self.prompt = search, model, prompt
        self.context_chars = context_chars
        self.counter, self.max_input_tokens = counter, max_input_tokens

    async def ask(self, kb_id: str, query: str, **options: Any) -> dict[str, Any]:
        started = perf_counter()
        retrieval = await self.search.search(kb_id, query, **options)
        evidence = []
        remaining = self.context_chars
        for candidate in retrieval["candidates"]:
            if remaining <= 0:
                break
            text = candidate["text"][:remaining]
            evidence.append({"chunk_id": candidate["chunk_id"], "text": text,
                             "sources": candidate["sources"]})
            remaining -= len(text)
        run_id = new_id("answer")
        repository = self.search.components.repository
        request = {"stage": "kb_qa", "system": self.prompt,
                   "question": query, "evidence": evidence}
        budget_request = {**request, "output_schema": KnowledgeAnswer.model_json_schema()}
        while evidence and self.counter(json.dumps(budget_request, ensure_ascii=False)) > (
            self.max_input_tokens
        ):
            evidence.pop()
        estimated = self.counter(json.dumps(budget_request, ensure_ascii=False))
        base = {
            "run_id": run_id, "search_run_id": retrieval["run_id"],
            "kb_id": kb_id, "snapshot_id": retrieval["snapshot_id"], "query": query,
            "document_ids": retrieval["document_ids"],
            "evidence": evidence, "prompt_sha256": fingerprint(self.prompt),
            "runtime": self.search.components.runtime_identity,
            "estimated_input_tokens": estimated,
            "retrieved_candidates": len(retrieval["candidates"]),
            "sent_candidates": len(evidence),
        }
        try:
            if estimated > self.max_input_tokens:
                raise ContextBudgetError(estimated, self.max_input_tokens)
            if not evidence:
                answer = KnowledgeAnswer(status="abstained", reason="指定范围没有预算内可用证据")
                usage = None
            else:
                report(self.search.progress, ProgressEvent("stage", "kb_answer"))
                response = await self.model.generate(request, KnowledgeAnswer)
                answer = KnowledgeAnswer.model_validate(response.output.model_dump())
                available = {item["chunk_id"]: item["text"] for item in evidence}
                for claim in answer.claims:
                    for citation in claim.citations:
                        if citation.quote not in available.get(citation.chunk_id, ""):
                            raise ValueError("回答引文不在实际发送的证据内")
                usage = response.usage.model_dump(mode="json")
            result = {
                **base, "answer": answer.model_dump(mode="json"), "usage": usage,
                "status": "completed", "total_seconds": perf_counter() - started,
            }
            repository.save_result(run_id, result)
            return result
        except Exception as error:
            repository.save_result(run_id, {
                **base, "status": "error", "error_type": type(error).__name__,
                "total_seconds": perf_counter() - started,
            })
            raise
