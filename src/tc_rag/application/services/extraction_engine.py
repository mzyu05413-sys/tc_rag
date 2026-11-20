"""结构化模型调用与并发提取：上下文预算、结果缓存、分批并发与证据校验。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, Protocol, TypeVar

from tc_rag.application.ports.contracts import (
    ModelResult,
    Repository,
    StructuredLLM,
    T,
)
from tc_rag.application.ports.errors import ContextBudgetError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.services.chunking import TokenCounter, normalized_quote
from tc_rag.application.services.design import check_extraction, issue
from tc_rag.application.services.effective_text import effective_block, effective_slice
from tc_rag.application.services.extraction_namespace import namespace_extraction
from tc_rag.application.services.generation_context import GenerationContextRecorder
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import Extraction, StageSnapshot

B = TypeVar("B")


class ExtractionRuntime(Protocol):
    """提取引擎所需的编排运行时视图；由 Pipeline 实现，读取并回写运行状态。"""

    config: AppConfig
    repository: Repository
    counter: TokenCounter
    max_context: int
    llm: StructuredLLM
    progress: ProgressReporter | None
    active_run_id: str
    resume_cache_run_id: str | None
    cache_hits: int

    def _check_budget(self, request: dict[str, Any]) -> None: ...

    async def _model(self, request: dict[str, Any], schema: type[T]) -> ModelResult: ...

    async def _extract_chunk(
        self, chunk: m.Chunk, blocks: list[m.DocumentBlock], request: dict[str, Any], run_id: str
    ) -> Extraction: ...


class ExtractionEngine:
    """封装模型调用、结果缓存与分批并发提取；运行生命周期仍由编排层持有。"""

    def __init__(self, runtime: ExtractionRuntime) -> None:
        self._rt = runtime

    def _check_budget(self, request: dict[str, Any]) -> None:
        estimated_tokens = self._rt.counter(json.dumps(request, ensure_ascii=False))
        if estimated_tokens > self._rt.max_context:
            raise ContextBudgetError(estimated_tokens, self._rt.max_context)

    async def _model(self, request: dict[str, Any], schema: type[T]) -> ModelResult:
        self._rt._check_budget({**request, "output_schema": schema.model_json_schema()})
        recorder = GenerationContextRecorder(self._rt.repository)
        context_id = recorder.start(
            self._rt.active_run_id, request, schema.__name__, self._rt.config.models.llm.model,
        )
        key = fingerprint(
            [
                request,
                schema.model_json_schema(),
                self._rt.config.models.llm.model_dump(mode="json"),
            ]
        )
        snapshot_id = f"{self._rt.active_run_id}:cache:{key}"
        if self._rt.config.runtime.cache_enabled:
            saved = self._rt.repository.get("stage_snapshots", snapshot_id, StageSnapshot)
            if saved is None and self._rt.resume_cache_run_id and request.get("stage") == "extract":
                parent = self._rt.repository.get(
                    "stage_snapshots", f"{self._rt.resume_cache_run_id}:cache:{key}", StageSnapshot
                )
                if parent is not None:
                    # 同一请求哈希才可复用；保存到新运行，以便再次恢复时保留成功批次。
                    schema.model_validate(parent.payload["output"])
                    saved = StageSnapshot(
                        snapshot_id=snapshot_id,
                        run_id=self._rt.active_run_id,
                        stage="model_cache",
                        payload=parent.payload,
                    )
                    self._rt.repository.save("stage_snapshots", snapshot_id, saved)
            if saved is not None:
                self._rt.cache_hits += 1
                report(
                    self._rt.progress,
                    ProgressEvent("cache", str(request.get("stage", "unknown"))),
                )
                # 缓存命中没有新增 token 消耗，历史消耗仍在 ModelInvocation 中。
                result = ModelResult(output=schema.model_validate(saved.payload["output"]))
                recorder.finish(context_id, "cache_hit")
                return result
        recorder.finish(context_id, "model_started")
        try:
            result = await self._rt.llm.generate(request, schema)
        except asyncio.CancelledError:
            recorder.finish(context_id, "cancelled")
            raise
        except Exception as exc:
            recorder.finish(context_id, "model_error", type(exc).__name__)
            raise
        recorder.finish(context_id, "model_completed")
        if self._rt.config.runtime.cache_enabled:
            snapshot = StageSnapshot(
                snapshot_id=snapshot_id,
                run_id=self._rt.active_run_id,
                stage="model_cache",
                payload={"output": result.output.model_dump(mode="json")},
            )
            self._rt.repository.save("stage_snapshots", snapshot_id, snapshot)
        return result

    async def _extract_concurrently(
        self,
        requests: list[tuple[m.Chunk, list[m.DocumentBlock], dict[str, Any]]],
        run_id: str,
    ) -> list[Extraction]:
        async def extract(
            item: tuple[m.Chunk, list[m.DocumentBlock], dict[str, Any]],
        ) -> Extraction:
            chunk, blocks, request = item
            try:
                return await self._rt._extract_chunk(chunk, blocks, request, run_id)
            except Exception as exc:
                if self._rt.config.runtime.chunk_failure_policy == "fail":
                    raise
                # skip 策略只记录固定类型，不把模型响应或原文写入终端。
                return Extraction(
                    issues=[
                        issue(
                            "other",
                            f"分块 {chunk.chunk_id} 提炼失败，已跳过：{type(exc).__name__}",
                        )
                    ]
                )

        return await self._batches(requests, extract, "extract")

    async def _batches(
        self, requests: list[B], operation: Callable[[B], Awaitable[T]], stage: str
    ) -> list[T]:
        """固定数量的工作协程，失败时取消并等待其余请求，按原文顺序归并。"""
        pending = iter(enumerate(requests))
        results: list[T | None] = [None] * len(requests)
        completed = 0

        async def worker() -> None:
            nonlocal completed
            for index, request in pending:
                started = perf_counter()
                report(
                    self._rt.progress,
                    ProgressEvent("batch_started", stage, current=index + 1, total=len(requests)),
                )
                results[index] = await operation(request)
                completed += 1
                report(
                    self._rt.progress,
                    ProgressEvent(
                        "batch_completed",
                        stage,
                        completed=completed,
                        total=len(requests),
                        duration_ms=(perf_counter() - started) * 1000,
                    ),
                )

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self._rt.config.models.llm.concurrency, len(requests)))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            # gather 不会自动收尾其他协程；必须避免失败后仍后台调用或写入缓存。
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        ordered: list[T] = []
        for result in results:
            if result is None:
                raise RuntimeError("批次任务结束但结果缺失")
            ordered.append(result)
        return ordered

    async def _extract_chunk(
        self, chunk: m.Chunk, blocks: list[m.DocumentBlock], request: dict[str, Any], run_id: str
    ) -> Extraction:
        result = await self._rt._model(request, Extraction)
        candidate = namespace_extraction(
            Extraction.model_validate(result.output.model_dump()), run_id + "_" + chunk.chunk_id
        )
        if request.get("neighbor_context_omitted"):
            candidate.issues.append(
                issue(
                    "gap",
                    "提炼邻文因上下文预算不足已省略；当前分块原文未裁减",
                    request["neighbor_context_omitted"],
                    severity="info",
                )
            )
        provenance = {b.block_id: b for b in blocks}
        for evidence in candidate.evidence:
            block = provenance.get(evidence.block_id)
            if block:
                # 来源取自解析事实，不允许模型把 vision 派生内容冒充原文。
                evidence.origin = block.origin
                evidence.processing_model = block.processing_model
                evidence.processing_version = block.processing_version
                evidence.uncertain = block.origin == "vision"
        scoped: dict[str, list[str]] = {}
        for source in chunk.sources:
            block = provenance[source.block_id]
            scoped.setdefault(source.block_id, []).append(
                effective_slice(
                    block.content, source.start_char, source.end_char, block.deprecated_ranges,
                )
            )
        rejected = [
            e.evidence_id
            for e in candidate.evidence
            if not any(
                normalized_quote(e.quote) in normalized_quote(text)
                for text in scoped.get(e.block_id, [])
            )
        ]
        if rejected and "neighbor_context" in request:
            candidate.issues.append(
                issue(
                    "validation",
                    "证据超出当前分块允许区间，邻文不能扩展引用范围",
                    rejected,
                )
            )
        candidate.evidence = [
            e
            for e in candidate.evidence
            if any(
                normalized_quote(e.quote) in normalized_quote(text)
                for text in scoped.get(e.block_id, [])
            )
        ]
        return check_extraction(candidate, [effective_block(b) for b in blocks])
