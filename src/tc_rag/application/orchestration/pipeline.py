"""RAG 连续编排：保存测试点事实后生成用例，联合导出目录脑图与 Excel。"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar

from tc_rag.application.ports.contracts import (
    ArtifactStore,
    DocumentParser,
    Exporter,
    ModelResult,
    PageOutcome,
    ParseState,
    PointMapAdapter,
    Repository,
    RetrievalContext,
    StructuredLLM,
    T,
)
from tc_rag.application.ports.errors import ContextBudgetError, OutputTruncatedError, WorkflowError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.requests import CasesRequest
from tc_rag.application.services.chunking import TokenCounter, structural_chunks
from tc_rag.application.services.design import (
    check_extraction,
    deduplicate_semantic,
    issue,
    plan_points,
    reconcile,
    validate_cases,
)
from tc_rag.application.services.ingest import ingest_document
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.pipeline import CaseBatch, Extraction, PointBatch, StageSnapshot
from tc_rag.domain.rag import RagRecord
from tc_rag.domain.review import PointNode, PointTree

B = TypeVar("B")


def save_objects(repository: Repository, collection: str, values: list[Any], key: str) -> None:
    repository.save_many((collection, str(getattr(value, key)), value) for value in values)


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_document_stem(name: str) -> str:
    """用需求文件名做导出的可读后缀，去掉文件系统非法字符并限制长度。"""
    stem = Path(name).stem.strip()
    stem = _INVALID_FILENAME.sub("_", stem).strip(" .")
    return stem[:60] or "requirement"


class Pipeline:
    """只依赖端口，解析器、模型、导出器都由装配层注入，默认测试不联网。"""

    def __init__(
        self,
        config: AppConfig,
        repository: Repository,
        store: ArtifactStore,
        parser: Callable[[str], DocumentParser],
        llm: StructuredLLM,
        maps: PointMapAdapter,
        exporter: Exporter,
        counter: TokenCounter,
        prompts: dict[str, str],
        *,
        retriever: RetrievalContext,
        max_context_tokens: int = 12000,
    ) -> None:
        if config.parsing.scan_policy != "report_unsupported":
            raise ValueError("整页 OCR 属于 P4，请使用 report_unsupported")
        self.config, self.repository, self.store = config, repository, store
        self.parser, self.llm, self.maps, self.exporter = parser, llm, maps, exporter
        self.counter, self.prompts, self.max_context = counter, prompts, max_context_tokens
        self.stages: dict[str, float] = {}
        self.previous_stage: str | None = None
        self.previous_started = perf_counter()
        self.active_run_id = ""
        self.resume_cache_run_id: str | None = None
        self.cache_hits = 0
        self.retriever = retriever
        self.vision: VisionEvidenceService | None = None
        self.progress: ProgressReporter | None = None

    def snapshot(self, run_id: str, stage: str, payload: dict[str, Any]) -> None:
        snapshot = StageSnapshot(
            snapshot_id=f"{run_id}:{stage}", run_id=run_id, stage=stage, payload=payload
        )
        self.repository.save("stage_snapshots", snapshot.snapshot_id, snapshot)

    def read(self, run_id: str, stage: str) -> dict[str, Any]:
        saved = self.repository.get("stage_snapshots", f"{run_id}:{stage}", StageSnapshot)
        if saved is None:
            raise WorkflowError("所需阶段尚未成功完成")
        return saved.payload

    def _run(self, run_id: str) -> m.Run:
        run = self.repository.get("runs", run_id, m.Run)
        if run is None:
            raise WorkflowError("运行 ID 不存在")
        return run

    def _save_run(self, run: m.Run, status: str, started: float) -> None:
        updated = m.Run.model_validate(
            {
                **run.model_dump(),
                "status": status,
                "timing": {
                    "duration_ms": (run.timing.duration_ms or 0) + (perf_counter() - started) * 1000
                },
            }
        )
        self.repository.save("runs", run.run_id, updated)

    def _stage(self, run_id: str, stage: str) -> None:
        now = perf_counter()
        if not self.stages:
            previous = self.repository.get("stage_snapshots", f"{run_id}:progress", StageSnapshot)
            if previous:
                self.stages.update(previous.payload.get("timings_ms", {}))
        if self.previous_stage and self.previous_stage not in {"points_ready", "completed"}:
            self.stages[self.previous_stage] = (
                self.stages.get(self.previous_stage, 0) + (now - self.previous_started) * 1000
            )
        self.previous_stage, self.previous_started = stage, now
        self.snapshot(run_id, "progress", {"current_stage": stage, "timings_ms": self.stages})
        report(self.progress, ProgressEvent("stage", stage))

    def _chunk(self, blocks: list[m.DocumentBlock]) -> tuple[list[m.Chunk], list[m.Issue]]:
        chunked = structural_chunks(
            blocks,
            self.config.chunking.target_tokens,
            self.config.chunking.overlap_tokens,
            self.counter,
        )
        return chunked.chunks, chunked.issues

    @staticmethod
    def _chunk_payload(
        doc: m.Document,
        blocks: list[m.DocumentBlock],
        chunks: list[m.Chunk],
        issues: list[m.Issue],
    ) -> dict[str, Any]:
        return {
            "document": doc.model_dump(mode="json"),
            "blocks": [b.model_dump(mode="json") for b in blocks],
            "chunks": [c.model_dump(mode="json") for c in chunks],
            "issues": [i.model_dump(mode="json") for i in issues],
        }

    async def _parse_document(
        self, run_id: str, document: Path, asset: m.Asset
    ) -> tuple[list[m.DocumentBlock], list[m.Asset], list[m.Issue]]:
        parser = self.parser(document.suffix.lower())
        session_factory = getattr(parser, "session", None)
        if document.suffix.lower() == ".pdf" and callable(session_factory):
            return await self._parse_pdf_pages(run_id, session_factory, asset)
        parsed = parser.parse(asset)
        blocks = list(parsed.blocks)
        issues = list(parsed.issues)
        if self.config.parsing.vision_enabled:
            if self.vision is None:
                raise ValueError("启用图片解释但缺少视觉适配器")
            blocks, vision_issues = await self.vision.process(blocks)
            issues.extend(vision_issues)
        return blocks, list(parsed.assets), issues

    async def _parse_pdf_pages(
        self, run_id: str, session_factory: Callable[[m.Asset], Any], asset: m.Asset
    ) -> tuple[list[m.DocumentBlock], list[m.Asset], list[m.Issue]]:
        """逐页解析并写检查点；恢复时从已完成页继续，已转写图片命中视觉缓存。

        视觉处理放在所有页解析完成之后统一执行，避免每页图片数少时跨页并发失效。
        """
        blocks: list[m.DocumentBlock] = []
        assets: list[m.Asset] = []
        parse_issues: list[m.Issue] = []
        with session_factory(asset) as session:
            state = ParseState(heading_sizes=list(session.heading_sizes))
            for page_index in range(session.page_count):
                page_no = page_index + 1
                current = self.repository.get(
                    "stage_snapshots", f"{run_id}:pdf_page:{page_no}", StageSnapshot
                )
                parent = None
                if current is None and self.resume_cache_run_id:
                    parent = self.repository.get(
                        "stage_snapshots",
                        f"{self.resume_cache_run_id}:pdf_page:{page_no}",
                        StageSnapshot,
                    )
                restored = current if current is not None else parent
                if restored is not None:
                    outcome = PageOutcome.from_payload(restored.payload)
                    if current is None:
                        # 复用父运行的页结果时同步写入本次运行，便于再次恢复。
                        self.snapshot(run_id, f"pdf_page:{page_no}", outcome.to_payload())
                else:
                    outcome = session.parse_page(page_index, state)
                    self.snapshot(run_id, f"pdf_page:{page_no}", outcome.to_payload())
                blocks.extend(outcome.blocks)
                assets.extend(outcome.assets)
                parse_issues.extend(outcome.issues)
                state = outcome.state
        if state.font_warnings:
            parse_issues.append(
                m.Issue(
                    issue_id=f"doc_{asset.checksum}_font_bbox",
                    kind="unparsed",
                    severity="warning",
                    object_ids=["doc_" + asset.checksum],
                    description=(
                        f"PDF字体边界信息缺失警告共{state.font_warnings}次，已合并重复终端提示；"
                        "正文解析继续，表格及文字位置需人工核对"
                    ),
                )
            )
        if self.config.parsing.vision_enabled and blocks:
            if self.vision is None:
                raise ValueError("启用图片解释但缺少视觉适配器")
            blocks, vision_issues = await self.vision.process(blocks)
            parse_issues.extend(vision_issues)
        # 视觉派生块会重编号，合并后统一恢复全局阅读顺序。
        ordered = [block.model_copy(update={"order": i}) for i, block in enumerate(blocks)]
        return ordered, assets, parse_issues

    async def points(
        self,
        document: Path,
        instruction: str,
        destination: Path,
        *,
        run_id: str | None = None,
        resume: str | None = None,
        from_stage: str | None = None,
        publish: bool = True,
    ) -> str:
        """保存测试点；正常全流程先不发布文件，诊断可单独导出测试点。"""
        if not instruction.strip():
            raise WorkflowError("生成指令不能为空")
        if from_stage and (not resume or from_stage not in {"extract", "plan_points"}):
            raise WorkflowError("--from-stage 需要 --resume，支持 extract/plan_points")
        run_id = run_id or new_id("run")
        self.active_run_id = run_id
        self.resume_cache_run_id = None
        run = m.Run(
            run_id=run_id, config_snapshot=self.config.model_dump(mode="json"), status="running"
        )
        self.repository.save("runs", run_id, run)
        started = perf_counter()
        stage = "ingest"
        issues: list[m.Issue] = []
        try:
            if resume:
                source = self._run(resume)
                if source.config_snapshot != run.config_snapshot:
                    raise WorkflowError("恢复运行需要保持配置一致")
                previous = self.read(resume, "input")
                if previous["instruction"] != instruction:
                    raise WorkflowError("恢复运行不能更换生成指令")
                ingested = ingest_document(document, self.store, self.repository)
                if ingested.document.content_hash != previous["document_fingerprint"]:
                    raise WorkflowError("恢复运行的文档内容已改变")
                # 配置、指令、文档都一致后，才复用父运行的提炼缓存与页检查点。
                self.resume_cache_run_id = resume
                doc = ingested.document
                self.snapshot(
                    run_id,
                    "input",
                    {"instruction": instruction, "document_fingerprint": doc.content_hash},
                )
                chunk_snapshot = self.repository.get(
                    "stage_snapshots", f"{resume}:chunk", StageSnapshot
                )
                if chunk_snapshot is not None:
                    parsed = chunk_snapshot.payload
                    blocks = [m.DocumentBlock.model_validate(b) for b in parsed["blocks"]]
                    chunks = [m.Chunk.model_validate(c) for c in parsed["chunks"]]
                    issues = [m.Issue.model_validate(i) for i in parsed["issues"]]
                    self.snapshot(run_id, "chunk", parsed)
                else:
                    # 缺 chunk 快照（曾在解析/视觉阶段中断）时重新解析：
                    # 已完成页从检查点恢复，已转写图片命中视觉缓存，不重复调用模型。
                    blocks, assets, parse_issues = await self._parse_document(
                        run_id, document, ingested.asset
                    )
                    issues.extend(parse_issues)
                    save_objects(self.repository, "assets", assets, "asset_id")
                    save_objects(self.repository, "document_blocks", blocks, "block_id")
                    stage = "chunk"
                    self._stage(run_id, stage)
                    chunks, chunk_issues = self._chunk(blocks)
                    issues.extend(chunk_issues)
                    save_objects(self.repository, "chunks", chunks, "chunk_id")
                    self.snapshot(
                        run_id, "chunk", self._chunk_payload(doc, blocks, chunks, issues)
                    )
            else:
                self._stage(run_id, stage)
                ingested = ingest_document(document, self.store, self.repository)
                doc = ingested.document
                # 提前写入 input 快照：解析中途中断也能通过 --resume 校验文档与指令。
                self.snapshot(
                    run_id,
                    "input",
                    {"instruction": instruction, "document_fingerprint": doc.content_hash},
                )
                stage = "parse"
                self._stage(run_id, stage)
                blocks, assets, parse_issues = await self._parse_document(
                    run_id, document, ingested.asset
                )
                issues.extend(parse_issues)
                save_objects(self.repository, "assets", assets, "asset_id")
                save_objects(self.repository, "document_blocks", blocks, "block_id")
                stage = "chunk"
                self._stage(run_id, stage)
                chunks, chunk_issues = self._chunk(blocks)
                issues.extend(chunk_issues)
                save_objects(self.repository, "chunks", chunks, "chunk_id")
                self.snapshot(run_id, "chunk", self._chunk_payload(doc, blocks, chunks, issues))
            stage = "extract"
            self._stage(run_id, stage)
            extracted: list[Extraction] = []
            if resume and from_stage == "plan_points":
                result = Extraction.model_validate(self.read(resume, "extract"))
            else:
                extraction_requests: list[
                    tuple[m.Chunk, list[m.DocumentBlock], dict[str, Any]]
                ] = []
                for chunk in chunks:
                    relevant = [
                        b for b in blocks if b.block_id in {s.block_id for s in chunk.sources}
                    ]
                    request = {
                        "stage": stage,
                        "system": self.prompts[stage],
                        "instruction": instruction,
                        "chunk": chunk.model_dump(mode="json"),
                        "document_blocks": [b.model_dump(mode="json") for b in relevant],
                    }
                    # 分块正文很短也可能因来源元数据而超限。先检查所有完整请求，
                    # 避免前面的分块已消耗模型额度，后面的分块才发现无法发送。
                    self._check_budget({**request, "output_schema": Extraction.model_json_schema()})
                    extraction_requests.append((chunk, relevant, request))
                extracted = await self._extract_concurrently(extraction_requests, run_id)
                result = reconcile(extracted)
                if self.config.planning.semantic_dedupe_enabled:
                    result = await self._deduplicate_rules_semantically(result)
            self.snapshot(run_id, "extract", result.model_dump(mode="json"))
            stage = "index"
            self._stage(run_id, stage)
            await self.retriever.prepare(
                doc.content_hash, self._retrieval_evidence(result.evidence, blocks, chunks)
            )
            for collection, values, key in (
                ("evidence_spans", result.evidence, "evidence_id"),
                ("features", result.features, "feature_id"),
                ("requirement_rules", result.rules, "rule_id"),
                ("business_entities", result.entities, "entity_id"),
            ):
                save_objects(self.repository, collection, values, key)
            issues.extend(result.issues)
            stage = "plan_points"
            self._stage(run_id, stage)
            points = plan_points(result.rules, self.config.planning.methods)

            async def plan_batch(rule_batch: list[m.RequirementRule]) -> PointBatch:
                allowed = {r.rule_id for r in rule_batch}
                evidence_ids = {e for r in rule_batch for e in r.evidence_ids}
                request = {
                    "stage": stage,
                    "system": self.prompts[stage],
                    "instruction": instruction,
                    "rules": [r.model_dump(mode="json") for r in rule_batch],
                    "evidence": [
                        e.model_dump(mode="json")
                        for e in result.evidence
                        if e.evidence_id in evidence_ids
                    ],
                    "existing_points": [
                        p.model_dump(mode="json") for p in points if set(p.rule_ids) <= allowed
                    ],
                }
                await self._supplement(
                    request,
                    evidence_ids,
                    instruction + "\n" + "\n".join(r.statement for r in rule_batch),
                    PointBatch,
                )
                self._check_budget(request)
                supplement = PointBatch.model_validate(
                    (await self._model(request, PointBatch)).output.model_dump()
                )
                if len(supplement.points) > self.config.planning.max_points_per_rule * len(
                    rule_batch
                ):
                    raise WorkflowError("模型补充测试点超过配置数量上限")
                if any(not set(point.rule_ids) <= allowed for point in supplement.points):
                    raise WorkflowError("模型测试点引用当前批次外规则")
                return supplement

            size = self.config.generation.batch_point_limit
            rule_batches = [
                result.rules[offset : offset + size] for offset in range(0, len(result.rules), size)
            ]
            supplements = await self._batches(rule_batches, plan_batch, stage)
            # 各批检索与调用可以并发，归并仍严格按规则的原始顺序进行。
            for rule_batch, supplement in zip(rule_batches, supplements, strict=True):
                for point in supplement.points:
                    if point.technique == "boundary":
                        issues.append(
                            issue("validation", "模型边界补充被忽略，数值边界仅由程序推导")
                        )
                        continue
                    if (
                        point.technique not in self.config.planning.methods
                        and point.technique not in {"workflow", "error_behavior", "other"}
                    ):
                        continue
                    point.point_id = new_id("point")
                    if any(
                        r.status in {"ambiguous", "conflicting"}
                        for r in rule_batch
                        if r.rule_id in point.rule_ids
                    ):
                        point.status = "needs_clarification"
                    if not any(
                        p.rule_ids == point.rule_ids
                        and p.technique == point.technique
                        and p.objective == point.objective
                        for p in points
                    ):
                        points.append(point)
                issues.extend(supplement.issues)
            if not points:
                issues.append(issue("gap", "没有得到测试点，请检查解析和模型输出"))
            save_objects(self.repository, "test_points", points, "point_id")
            tree = self._tree(run_id, doc.content_hash, result, points, blocks)
            self.repository.save("point_trees", tree.tree_id, tree)
            self.snapshot(
                run_id,
                "plan_points",
                {"tree_id": tree.tree_id, "points": [p.model_dump(mode="json") for p in points]},
            )
            save_objects(self.repository, "issues", issues, "issue_id")
            self.snapshot(run_id, "issues", {"items": [i.model_dump(mode="json") for i in issues]})
            target = destination / run_id
            payload = {
                "schema_version": "1.0",
                "run_id": run_id,
                "mode": "rag",
                "document": doc.model_dump(mode="json"),
                **result.model_dump(mode="json"),
                "points": [p.model_dump(mode="json") for p in points],
                "tree": tree.model_dump(mode="json"),
                "issues": [i.model_dump(mode="json") for i in issues],
            }
            if publish:
                stage = "export_points"
                self._stage(run_id, stage)
                stem = safe_document_stem(doc.name)
                self.maps.export(tree, target / f"testpoints_{stem}.xmind")
                if "json" in self.config.export.formats:
                    self.exporter.export(self._view(payload), target / "testpoints.json")
                if "markdown" in self.config.export.formats:
                    self.exporter.export(self._view(payload), target / "report.md")
                self.snapshot(
                    run_id, "points_output", {"directory": str(target), "payload": payload}
                )
            self._stage(run_id, "points_ready")
            self.snapshot(run_id, "manifest", self.manifest(run_id))
            if publish:
                self.exporter.export(self.manifest(run_id), target / "manifest.json")
                self.exporter.export(
                    self.manifest(run_id)["metrics"], target / "reports" / "metrics.json"
                )
            status = ("partial_failed" if issues else "succeeded") if publish else "running"
            self._save_run(run, status, started)
            return run_id
        except BaseException as exc:
            self.snapshot(run_id, "failure", {"stage": stage, "error_type": type(exc).__name__})
            self._save_run(
                run, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", started
            )
            raise

    async def generate(
        self,
        document: Path,
        instruction: str,
        destination: Path,
        *,
        run_id: str | None = None,
        resume: str | None = None,
        from_stage: str | None = None,
    ) -> dict[str, Any]:
        """正式入口：测试点与用例均使用 RAG，途中无需审核或回读 XMind。"""
        run_id = await self.points(
            document,
            instruction,
            destination,
            run_id=run_id,
            resume=resume,
            from_stage=from_stage,
            publish=False,
        )
        return await self.cases(
            CasesRequest(
                run_id=run_id,
                instruction=instruction,
                output_dir=destination,
            )
        )

    def _check_budget(self, request: dict[str, Any]) -> None:
        import json

        estimated_tokens = self.counter(json.dumps(request, ensure_ascii=False))
        if estimated_tokens > self.max_context:
            raise ContextBudgetError(estimated_tokens, self.max_context)

    async def _model(self, request: dict[str, Any], schema: type[T]) -> ModelResult:
        self._check_budget({**request, "output_schema": schema.model_json_schema()})
        key = fingerprint(
            [request, schema.model_json_schema(), self.config.models.llm.model_dump(mode="json")]
        )
        snapshot_id = f"{self.active_run_id}:cache:{key}"
        if self.config.runtime.cache_enabled:
            saved = self.repository.get("stage_snapshots", snapshot_id, StageSnapshot)
            if saved is None and self.resume_cache_run_id and request.get("stage") == "extract":
                parent = self.repository.get(
                    "stage_snapshots", f"{self.resume_cache_run_id}:cache:{key}", StageSnapshot
                )
                if parent is not None:
                    # 同一请求哈希才可复用；保存到新运行，以便再次恢复时保留成功批次。
                    schema.model_validate(parent.payload["output"])
                    saved = StageSnapshot(
                        snapshot_id=snapshot_id,
                        run_id=self.active_run_id,
                        stage="model_cache",
                        payload=parent.payload,
                    )
                    self.repository.save("stage_snapshots", snapshot_id, saved)
            if saved is not None:
                self.cache_hits += 1
                report(self.progress, ProgressEvent("cache", str(request.get("stage", "unknown"))))
                # 缓存命中没有新增 token 消耗，历史消耗仍在 ModelInvocation 中。
                return ModelResult(output=schema.model_validate(saved.payload["output"]))
        result = await self.llm.generate(request, schema)
        if self.config.runtime.cache_enabled:
            snapshot = StageSnapshot(
                snapshot_id=snapshot_id,
                run_id=self.active_run_id,
                stage="model_cache",
                payload={"output": result.output.model_dump(mode="json")},
            )
            self.repository.save("stage_snapshots", snapshot_id, snapshot)
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
            return await self._extract_chunk(chunk, blocks, request, run_id)

        return await self._batches(requests, extract, "extract")

    async def _deduplicate_rules_semantically(self, result: Extraction) -> Extraction:
        """显式开启时，对归并后的规则做严格保守的语义去重。

        每条规则的 statement 编码一次向量（走向量缓存，不重复请求），在
        同功能/同类型/同结构化约束内按余弦相似度合并近似重复并合并证据并集。
        仅记录被合并的规则映射和一条提示，不改变 evidence/features/entities。
        """
        rules = result.rules
        threshold = float(self.config.planning.semantic_dedupe_threshold)
        # 并发取向量；失败只跳过该规则的合并（保留原样），不影响主流程。
        vectors: dict[str, list[float]] = {}

        async def embed_one(rule: m.RequirementRule) -> None:
            try:
                vector, _ = await self.retriever.vector(rule.statement)
                vectors[rule.rule_id] = vector
            except Exception:
                # 缺向量的规则直接跳过合并，保守保留，不因单个失败中断运行。
                return

        if rules:
            await asyncio.gather(*(embed_one(r) for r in rules))
        deduped = deduplicate_semantic(rules, vectors, threshold)
        if len(deduped.rules) != len(rules):
            merged_count = len(rules) - len(deduped.rules)
            result = result.model_copy(
                update={
                    "rules": deduped.rules,
                    "rule_mapping": {
                        **result.rule_mapping,
                        **{
                            k: v
                            for k, v in deduped.rule_mapping.items()
                            if k != v
                        },
                    },
                    "issues": result.issues
                    + [
                        issue(
                            "validation",
                            f"语义去重合并 {merged_count} 条近似规则，保留证据并集",
                        )
                    ],
                }
            )
        return result

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
                    self.progress,
                    ProgressEvent("batch_started", stage, current=index + 1, total=len(requests)),
                )
                results[index] = await operation(request)
                completed += 1
                report(
                    self.progress,
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
            for _ in range(min(self.config.models.llm.concurrency, len(requests)))
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
        from tc_rag.application.services.chunking import normalized_quote

        result = await self._model(request, Extraction)
        candidate = self._namespace(
            Extraction.model_validate(result.output.model_dump()), run_id + "_" + chunk.chunk_id
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
            scoped.setdefault(source.block_id, []).append(
                provenance[source.block_id].content[source.start_char : source.end_char]
            )
        candidate.evidence = [
            e
            for e in candidate.evidence
            if any(
                normalized_quote(e.quote) in normalized_quote(text)
                for text in scoped.get(e.block_id, [])
            )
        ]
        return check_extraction(candidate, blocks)

    def _view(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if not self.config.export.include_issues:
            result.pop("issues", None)
        if not self.config.export.include_coverage:
            result.pop("coverage", None)
        return result

    def manifest(self, run_id: str) -> dict[str, Any]:
        invocations = self.repository.get("stage_snapshots", f"{run_id}:invocations", StageSnapshot)
        items = [] if invocations is None else invocations.payload["items"]
        usage: dict[str, int | None] = {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            values = [item["usage"][field] for item in items]
            usage[field] = sum(values) if values and all(v is not None for v in values) else None
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "mode": "rag",
            "config": self._run(run_id).config_snapshot,
            "prompts": {
                key: {"version": "v1", "fingerprint": fingerprint(value)}
                for key, value in self.prompts.items()
            },
            "input": self.read(run_id, "input"),
            "metrics": {
                "stage_ms": self.stages,
                "actual_model_calls": len(items),
                "cache_hits_this_execution": self.cache_hits,
                "usage": usage,
                "failed_calls": sum(i["error"] is not None for i in items),
                "schema_failed_calls": sum(i["error"] == "SchemaOutputError" for i in items),
                "retryable_failed_calls": sum(
                    i["error"]
                    in {
                        "HTTP_408",
                        "HTTP_409",
                        "HTTP_429",
                        "APIConnectionError",
                        "APITimeoutError",
                        "TimeoutError",
                    }
                    or str(i["error"]).startswith("HTTP_5")
                    for i in items
                ),
            },
            "limitations": [
                "补充证据需人工核对",
                "自动检查不替代语义审核",
            ],
        }

    @staticmethod
    def _namespace(candidate: Extraction, prefix: str) -> Extraction:
        """把模型局部 ID 映射到当前运行；不同类型允许复用同一个本地 ID。"""
        data = candidate.model_dump(mode="json")
        collections = (
            ("evidence", "evidence_id"),
            ("features", "feature_id"),
            ("rules", "rule_id"),
            ("entities", "entity_id"),
            ("issues", "issue_id"),
        )
        mappings: dict[str, dict[str, str]] = {}
        for collection, key in collections:
            mapping: dict[str, str] = {}
            for obj in data[collection]:
                old = obj[key]
                if old in mapping:
                    raise WorkflowError(f"模型在当前批次返回了重复的 {key}")
                mapping[old] = f"{prefix}_{collection}_{old}"
            mappings[collection] = mapping
        # object_ids 可能引用任意类型；只有归属唯一时才改写，避免猜错对象。
        owners: dict[str, list[str]] = {}
        for mapping in mappings.values():
            for old, new in mapping.items():
                owners.setdefault(old, []).append(new)
        cross = {old: values[0] for old, values in owners.items() if len(values) == 1}
        field_collection = {
            "evidence_id": "evidence",
            "feature_id": "features",
            "rule_id": "rules",
            "entity_id": "entities",
            "issue_id": "issues",
        }
        for collection in data.values():
            if not isinstance(collection, list):
                continue
            for obj in collection:
                for field, value in list(obj.items()):
                    if field == "evidence_ids":
                        mapping = mappings["evidence"]
                        obj[field] = [mapping.get(v, v) for v in value]
                    elif field in field_collection:
                        mapping = mappings[field_collection[field]]
                        obj[field] = mapping.get(value, value)
                    elif field == "object_ids":
                        obj[field] = [cross.get(v, v) for v in value]
        return Extraction.model_validate(data)

    @staticmethod
    def _tree(
        run_id: str,
        checksum: str,
        extraction: Extraction,
        points: list[m.TestPoint],
        blocks: list[m.DocumentBlock],
    ) -> PointTree:
        """按原文章节 heading_path 组织功能层级；同一路径复用节点，重复功能自然合并。

        测试点经 rule→evidence→block 找到所在章节路径，挂到最深层分组下；
        原文没有标题层级时回退到按功能名分组，保持扁平但可读。
        """
        root = new_id("node")
        nodes = [PointNode(node_id=root, title="需求测试点", kind="group", order=0)]
        block_heading = {b.block_id: b.heading_path for b in blocks}
        evidence_block = {e.evidence_id: e.block_id for e in extraction.evidence}
        rule_evidence = {r.rule_id: r.evidence_ids for r in extraction.rules}
        feature_name = {f.feature_id: f.name for f in extraction.features}
        rule_feature = {r.rule_id: r.feature_id for r in extraction.rules}
        path_nodes: dict[tuple[str, ...], str] = {(): root}
        next_order: dict[str, int] = {root: 0}

        def new_group(parent_id: str, title: str) -> str:
            order = next_order.get(parent_id, 0)
            node_id = new_id("node")
            nodes.append(
                PointNode(
                    node_id=node_id, parent_id=parent_id, title=title, kind="group", order=order
                )
            )
            next_order[parent_id] = order + 1
            next_order[node_id] = 0
            return node_id

        def ensure_group(path: tuple[str, ...]) -> str:
            parent = root
            prefix: tuple[str, ...] = ()
            for title in path:
                prefix = prefix + (title,)
                node_id = path_nodes.get(prefix)
                if node_id is None:
                    node_id = new_group(parent, title)
                    path_nodes[prefix] = node_id
                parent = node_id
            return parent

        def point_path(point: m.TestPoint) -> tuple[str, ...]:
            for rule_id in point.rule_ids:
                for evidence_id in rule_evidence.get(rule_id, []):
                    block_id = evidence_block.get(evidence_id)
                    heading = block_heading.get(block_id, []) if block_id else []
                    if heading:
                        return tuple(heading)
            for rule_id in point.rule_ids:
                name = feature_name.get(rule_feature.get(rule_id, ""), "")
                if name:
                    return (name,)
            return ()

        assigned: set[str] = set()
        for point in points:
            if point.point_id in assigned:
                continue
            assigned.add(point.point_id)
            parent = ensure_group(point_path(point))
            order = next_order.get(parent, 0)
            nodes.append(
                PointNode(
                    node_id=new_id("node"),
                    parent_id=parent,
                    title=point.objective,
                    kind="point",
                    order=order,
                    point_id=point.point_id,
                    technique=point.technique,
                )
            )
            next_order[parent] = order + 1
        return PointTree(
            tree_id=new_id("tree"),
            version=1,
            run_id=run_id,
            document_fingerprint=checksum,
            nodes=nodes,
        )

    async def cases(
        self,
        request: CasesRequest,
    ) -> dict[str, Any]:
        """只从 SQLite 读取当前运行的测试点与证据，不接受脑图反向修改业务事实。"""
        run = self._run(request.run_id)
        if run.config_snapshot != self.config.model_dump(mode="json"):
            raise WorkflowError("配置已变化；请使用保存测试点时的配置，或重新执行完整流程")
        self.active_run_id = request.run_id
        planned = self.read(request.run_id, "plan_points")
        tree = self.repository.get("point_trees", planned["tree_id"], PointTree)
        if tree is None or tree.run_id != request.run_id:
            raise WorkflowError("测试点树不存在或不属于当前运行")
        if tree.document_fingerprint != self.read(request.run_id, "input")["document_fingerprint"]:
            raise WorkflowError("测试点与需求文档版本不一致")
        self.snapshot(request.run_id, "generation_request", request.model_dump(mode="json"))
        self.snapshot(
            request.run_id,
            "skill",
            {
                "integration": "markdown_instructions_only",
                "builtin_checksum": fingerprint(self.prompts.get("case_skill", "")),
                "custom_checksum": fingerprint(self.prompts["custom_case_skill"])
                if "custom_case_skill" in self.prompts
                else None,
            },
        )
        extracted = Extraction.model_validate(self.read(request.run_id, "extract"))
        baseline = [m.TestPoint.model_validate(p) for p in planned["points"]]
        if {n.point_id for n in tree.nodes if n.kind == "point"} != {p.point_id for p in baseline}:
            raise WorkflowError("测试点树与保存的测试点事实不一致")
        if any(not set(p.rule_ids) <= {r.rule_id for r in extracted.rules} for p in baseline):
            raise WorkflowError("测试点包含当前需求之外的规则引用")
        points = [p for p in baseline if p.status == "ready" and p.applicability == "applicable"]
        started = perf_counter()
        self.repository.save("runs", run.run_id, run.model_copy(update={"status": "running"}))
        stage = "generate"
        try:
            stage = "index"
            self._stage(run.run_id, stage)
            parsed = self.read(run.run_id, "chunk")
            stem = safe_document_stem(parsed.get("document", {}).get("name", "requirement"))
            xmind_name = f"testpoints_{stem}.xmind"
            excel_name = f"testcases_{stem}.xlsx"
            await self.retriever.prepare(
                tree.document_fingerprint,
                self._retrieval_evidence(
                    extracted.evidence,
                    [m.DocumentBlock.model_validate(b) for b in parsed["blocks"]],
                    [m.Chunk.model_validate(c) for c in parsed["chunks"]],
                ),
            )
            stage = "generate"
            self._stage(run.run_id, stage)
            cases: list[m.TestCase] = []
            issues = [m.Issue.model_validate(i) for i in self.read(run.run_id, "issues")["items"]]
            batch_size = self.config.generation.batch_point_limit
            point_batches = [
                points[offset : offset + batch_size] for offset in range(0, len(points), batch_size)
            ]

            async def generate_batch(batch: list[m.TestPoint]) -> CaseBatch:
                return await self._generate_batch(request.instruction, batch, extracted)

            generated_batches = await self._batches(point_batches, generate_batch, stage)
            for generated in generated_batches:
                for case in generated.cases:
                    case.case_id = new_id("case")
                cases.extend(generated.cases)
                issues.extend(generated.issues)
            stage = "validate"
            self._stage(run.run_id, stage)
            cases, links, validation = validate_cases(
                cases, points, extracted.rules, extracted.evidence
            )
            issues.extend(validation)
            if not points or not cases:
                issues.append(issue("gap", "没有可生成测试点或有效用例，不能作为完整交付"))
            # 待澄清的测试点不偷偷忽略，报告中列出不能生成的原因。
            generated_ids = {p.point_id for p in points}
            for node in tree.nodes:
                if node.kind == "point" and node.point_id not in generated_ids:
                    issues.append(
                        issue(
                            "gap",
                            "测试点不适用或仍待澄清，未生成用例",
                            [node.point_id or node.node_id],
                        )
                    )
            save_objects(self.repository, "test_cases", cases, "case_id")
            self.repository.save_many(
                (
                    "coverage_links",
                    fingerprint([link.case_id, link.step_no, link.rule_id, link.point_id]),
                    link,
                )
                for link in links
            )
            save_objects(self.repository, "issues", issues, "issue_id")
            payload = {
                "schema_version": "1.0",
                "run_id": run.run_id,
                "mode": "rag",
                "cases": [c.model_dump(mode="json") for c in cases],
                "points": [p.model_dump(mode="json") for p in baseline],
                "evidence": [e.model_dump(mode="json") for e in extracted.evidence],
                "features": [f.model_dump(mode="json") for f in extracted.features],
                "rules": [r.model_dump(mode="json") for r in extracted.rules],
                "coverage": [link.model_dump(mode="json") for link in links],
                "issues": [i.model_dump(mode="json") for i in issues],
                "quality": {
                    "points": len(points),
                    "cases": len(cases),
                    "covered_points": len({link.point_id for link in links}),
                    "semantic_review": "not_performed",
                },
            }
            # XMind 是 Excel 用例的目录：只保留实际导出用例所对应的测试点及其分组。
            directory_tree = self._case_directory(tree, {p for c in cases for p in c.point_ids})
            payload["tree"] = directory_tree.model_dump(mode="json")
            self.snapshot(run.run_id, "cases", payload)
            stage = "export_cases"
            self._stage(run.run_id, stage)
            target = request.output_dir / run.run_id
            # 重试或重新生成使用新批次，保证每个目录中的 XMind 与 Excel 来自同一批事实。
            if self.repository.get("stage_snapshots", f"{run.run_id}:cases_output", StageSnapshot):
                target /= new_id("batch")
            self.maps.export(directory_tree, target / xmind_name)
            for format_name, name in (
                ("json", "testcases.json"),
                ("markdown", "report.md"),
                ("xlsx", excel_name),
            ):
                if format_name == "xlsx" or format_name in self.config.export.formats:
                    self.exporter.export(self._view(payload), target / name)
            self.exporter.export(
                {
                    "quality": payload["quality"],
                    "coverage": payload["coverage"],
                    "issues": payload["issues"],
                },
                target / "reports" / "quality.md",
            )
            self.snapshot(run.run_id, "cases_output", {"directory": str(target)})
            self._stage(run.run_id, "completed")
            self.snapshot(run.run_id, "manifest", self.manifest(run.run_id))
            self.exporter.export(self.manifest(run.run_id), target / "manifest.json")
            self.exporter.export(
                self.manifest(run.run_id)["metrics"], target / "reports" / "metrics.json"
            )
            self._save_run(run, "partial_failed" if issues else "succeeded", started)
            return {
                "run_id": run.run_id,
                "directory": str(target),
                "xmind": str(target / xmind_name),
                "excel": str(target / excel_name),
                "cases": len(cases),
                "issues": len(issues),
            }
        except BaseException as exc:
            self.snapshot(run.run_id, "failure", {"stage": stage, "error_type": type(exc).__name__})
            self._save_run(
                run, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", started
            )
            raise

    @staticmethod
    def _case_directory(tree: PointTree, point_ids: set[str]) -> PointTree:
        """保留用例所对应的叶子和祖先；不把步骤、预期或缺失用例的点写入目录。"""
        nodes = {n.node_id: n for n in tree.nodes}
        keep = {n.node_id for n in tree.nodes if n.parent_id is None}
        for leaf in tree.nodes:
            if leaf.kind != "point" or leaf.point_id not in point_ids:
                continue
            node: PointNode | None = leaf
            while node is not None:
                keep.add(node.node_id)
                node = nodes.get(node.parent_id or "")
        return tree.model_copy(update={"nodes": [n for n in tree.nodes if n.node_id in keep]})

    async def _supplement(
        self, request: dict[str, Any], direct_ids: set[str], query: str, schema: type[T]
    ) -> None:
        started = perf_counter()
        bundle = await self.retriever.retrieve(query, direct_ids)
        # 仅上报计数和固定状态，不向终端暴露查询、证据或供应商响应正文。
        report(
            self.progress,
            ProgressEvent(
                "retrieval_completed",
                str(request.get("stage", "unknown")),
                rerank_status=bundle.get("rerank", {}).get("status"),
            ),
        )
        supplement = list(bundle["supplemental_evidence"])
        omitted = []
        while supplement:
            request["supplemental_evidence"] = supplement
            try:
                self._check_budget({**request, "output_schema": schema.model_json_schema()})
                break
            except ValueError:
                omitted.append(supplement.pop()["evidence_id"])
        request["supplemental_evidence"] = supplement
        bundle["adopted_ids"] = [e["evidence_id"] for e in supplement]
        bundle["omitted_ids"] = omitted
        bundle["generation"] = {
            "stage": request.get("stage"),
            "schema": schema.__name__,
            "max_context_tokens": self.max_context,
            "model": self.config.models.llm.model,
        }
        bundle["timings_ms"]["context_build_ms"] += (perf_counter() - started) * 1000 - sum(
            bundle["timings_ms"].values()
        )
        key = f"{self.active_run_id}:retrieval"
        previous = self.repository.get("stage_snapshots", key, StageSnapshot)
        bundles = previous.payload["bundles"] if previous else []
        bundles.append(bundle)
        self.snapshot(self.active_run_id, "retrieval", {"bundles": bundles})
        # 保存最终采用/省略结果，不能只留下裁剪前的原始命中。
        key = "context_" + fingerprint([self.active_run_id, query, sorted(direct_ids)])
        self.repository.save(
            "context_bundles", key, RagRecord(record_id=key, kind="context", payload=bundle)
        )

    @staticmethod
    def _retrieval_evidence(
        evidence: list[m.EvidenceSpan], blocks: list[m.DocumentBlock], chunks: list[m.Chunk]
    ) -> list[m.EvidenceSpan]:
        """原文片段也入检索库，避免提炼遗漏限制召回；不把片段自动登记为规则。"""
        result = {e.evidence_id: e for e in evidence}
        block_map = {b.block_id: b for b in blocks}
        for chunk in chunks:
            for source in chunk.sources:
                block = block_map[source.block_id]
                key = "source_" + fingerprint(source.model_dump(mode="json"))
                result[key] = m.EvidenceSpan(
                    evidence_id=key,
                    block_id=block.block_id,
                    locator=block.locator,
                    quote=block.content[source.start_char : source.end_char],
                    origin=block.origin,
                    processing_model=block.processing_model,
                    processing_version=block.processing_version,
                )
        return list(result.values())

    async def _generate_batch(
        self,
        instruction: str,
        points: list[m.TestPoint],
        extracted: Extraction,
    ) -> CaseBatch:
        rule_ids = {r for p in points for r in p.rule_ids}
        rules = [r for r in extracted.rules if r.rule_id in rule_ids]
        # 规则绑定的原文证据优先；补齐所属功能和关联规则的证据，保留条件与例外。
        feature_ids = {r.feature_id for r in rules}
        contextual_rules = [r for r in extracted.rules if r.feature_id in feature_ids]
        evidence_ids = {e for r in contextual_rules for e in r.evidence_ids}
        request = {
            "stage": "generate",
            "system": self.prompts["generate"],
            "instruction": instruction,
            "points": [p.model_dump(mode="json") for p in points],
            "rules": [r.model_dump(mode="json") for r in contextual_rules],
            "evidence": [
                e.model_dump(mode="json")
                for e in extracted.evidence
                if e.evidence_id in evidence_ids
            ],
        }
        try:
            self._check_budget({**request, "output_schema": CaseBatch.model_json_schema()})
        except ValueError:
            if len(points) == 1:
                raise
            middle = len(points) // 2
            left = await self._generate_batch(instruction, points[:middle], extracted)
            right = await self._generate_batch(instruction, points[middle:], extracted)
            return CaseBatch(cases=left.cases + right.cases, issues=left.issues + right.issues)
        await self._supplement(
            request, evidence_ids, "\n".join(p.objective for p in points), CaseBatch
        )
        try:
            model_result = await self._model(request, CaseBatch)
        except OutputTruncatedError:
            if len(points) == 1:
                raise
            middle = len(points) // 2
            left = await self._generate_batch(instruction, points[:middle], extracted)
            right = await self._generate_batch(instruction, points[middle:], extracted)
            return CaseBatch(cases=left.cases + right.cases, issues=left.issues + right.issues)
        candidate = CaseBatch.model_validate(model_result.output.model_dump())
        if len(candidate.cases) > len(points) * self.config.generation.max_cases_per_point:
            raise WorkflowError("模型返回用例超过当前批次数量上限")
        allowed = {p.point_id for p in points}
        if any(not set(c.point_ids) <= allowed for c in candidate.cases):
            raise WorkflowError("模型生成了不属于当前批次的测试点引用")
        if any(
            sum(point_id in c.point_ids for c in candidate.cases)
            > self.config.generation.max_cases_per_point
            for point_id in allowed
        ):
            raise WorkflowError("单个测试点用例数量超过配置上限")
        return candidate
