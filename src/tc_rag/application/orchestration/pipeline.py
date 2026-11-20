"""RAG 连续编排：保存测试点事实后生成用例，联合导出目录脑图与 Excel。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar
from typing import Literal as Literal  # 保留旧模块可导入的类型名。

from tc_rag.application.orchestration.support import (
    assign_module_paths,
    safe_document_stem,
    save_objects,
)
from tc_rag.application.orchestration.support import (
    effective_chunk as _effective_chunk,
)
from tc_rag.application.orchestration.support import (
    final_status as _final_status,
)
from tc_rag.application.ports.contracts import (
    ArtifactStore,
    DocumentParser,
    Exporter,
    ModelResult,
    PointMapAdapter,
    Repository,
    RetrievalContext,
    StructuredLLM,
    T,
)
from tc_rag.application.ports.errors import WorkflowError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.requests import CasesRequest
from tc_rag.application.services.case_generation import CaseGenerator
from tc_rag.application.services.chunking import TokenCounter
from tc_rag.application.services.design import (
    apply_vision_policy,
    boundary_values,
    build_plan_query,
    catalog_feature_candidates,
    combination_points,
    dedupe_issues,
    deduplicate_points,
    drop_resolved_image_issues,
    error_rule_ids,
    gap_report,
    group_by_feature,
    is_ui_display_rule,
    issue,
    merge_ui_display_points,
    plan_points,
    plan_ui_display_summary,
    qualitative_boundary_values,
    reconcile,
    validate_cases,
)
from tc_rag.application.services.effective_text import effective_block, effective_slice
from tc_rag.application.services.extraction_context import (
    CONTEXT_POLICY,
    fit_context_budget,
    neighboring_context,
)
from tc_rag.application.services.extraction_engine import ExtractionEngine

# 保留旧模块的纯函数和常量导入入口。
from tc_rag.application.services.extraction_namespace import namespace_extraction
from tc_rag.application.services.ingest import ingest_document
from tc_rag.application.services.ingest_flow import DocumentIngestHandler
from tc_rag.application.services.point_tree import CATEGORY_ORDER as CATEGORY_ORDER
from tc_rag.application.services.point_tree import ENTRY_HINTS as ENTRY_HINTS
from tc_rag.application.services.point_tree import SECURITY_HINTS as SECURITY_HINTS
from tc_rag.application.services.point_tree import STATUS_LABELS as STATUS_LABELS
from tc_rag.application.services.point_tree import STATUS_ORDER as STATUS_ORDER
from tc_rag.application.services.point_tree import (
    build_point_tree,
    point_category,
    sort_points,
)
from tc_rag.application.services.point_tree import point_feature as point_feature
from tc_rag.application.services.vision import VisionEvidenceService
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.pipeline import (
    CaseBatch,
    Extraction,
    PointBatch,
    StageSnapshot,
)
from tc_rag.domain.review import PointTree

B = TypeVar("B")


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
        prompt_versions: dict[str, str] | None = None,
    ) -> None:
        self.config, self.repository, self.store = config, repository, store
        self.llm, self.maps, self.exporter = llm, maps, exporter
        self.counter, self.prompts, self.max_context = counter, prompts, max_context_tokens
        self.prompt_versions = prompt_versions or {}
        self.stages: dict[str, float] = {}
        self.previous_stage: str | None = None
        self.previous_started = perf_counter()
        self.active_run_id = ""
        self.resume_cache_run_id: str | None = None
        self.cache_hits = 0
        self.retriever = retriever
        self.ingest = DocumentIngestHandler(
            config,
            repository,
            parser,
            counter,
            stage=self._stage,
            snapshot=self.snapshot,
        )
        self.progress: ProgressReporter | None = None
        self.engine = ExtractionEngine(self)
        self.generator = CaseGenerator(self)

    @property
    def vision(self) -> VisionEvidenceService | None:
        return self.ingest.vision

    @vision.setter
    def vision(self, value: VisionEvidenceService | None) -> None:
        self.ingest.vision = value

    @property
    def parser(self) -> Callable[[str], DocumentParser]:
        return self.ingest.parser

    @parser.setter
    def parser(self, value: Callable[[str], DocumentParser]) -> None:
        self.ingest.parser = value

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
                    issues = drop_resolved_image_issues(issues, blocks)
                    self.snapshot(run_id, "chunk", parsed)
                else:
                    # 缺 chunk 快照（曾在解析/视觉阶段中断）时重新解析：
                    # 已完成页从检查点恢复，已转写图片命中视觉缓存，不重复调用模型。
                    blocks, assets, parse_issues = await self.ingest._parse_document(
                        run_id, document, ingested.asset, self.resume_cache_run_id
                    )
                    issues.extend(parse_issues)
                    blocks, vision_issues = await self.ingest._apply_vision(run_id, blocks)
                    issues.extend(vision_issues)
                    issues = drop_resolved_image_issues(issues, blocks)
                    save_objects(self.repository, "assets", assets, "asset_id")
                    save_objects(self.repository, "document_blocks", blocks, "block_id")
                    stage = "chunk"
                    self._stage(run_id, stage)
                    chunks, chunk_issues = self.ingest._chunk(blocks)
                    issues.extend(chunk_issues)
                    save_objects(self.repository, "chunks", chunks, "chunk_id")
                    self.snapshot(
                        run_id, "chunk", self.ingest._chunk_payload(doc, blocks, chunks, issues)
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
                blocks, assets, parse_issues = await self.ingest._parse_document(
                    run_id, document, ingested.asset, self.resume_cache_run_id
                )
                issues.extend(parse_issues)
                blocks, vision_issues = await self.ingest._apply_vision(run_id, blocks)
                issues.extend(vision_issues)
                issues = drop_resolved_image_issues(issues, blocks)
                save_objects(self.repository, "assets", assets, "asset_id")
                save_objects(self.repository, "document_blocks", blocks, "block_id")
                stage = "chunk"
                self._stage(run_id, stage)
                chunks, chunk_issues = self.ingest._chunk(blocks)
                issues.extend(chunk_issues)
                save_objects(self.repository, "chunks", chunks, "chunk_id")
                self.snapshot(
                    run_id, "chunk", self.ingest._chunk_payload(doc, blocks, chunks, issues)
                )
            stage = "extract"
            self._stage(run_id, stage)
            extracted: list[Extraction] = []
            if resume and from_stage == "plan_points":
                result = Extraction.model_validate(self.read(resume, "extract"))
            else:
                extraction_requests: list[
                    tuple[m.Chunk, list[m.DocumentBlock], dict[str, Any]]
                ] = []
                block_map = {b.block_id: b for b in blocks}
                for chunk in chunks:
                    relevant = [
                        b for b in blocks if b.block_id in {s.block_id for s in chunk.sources}
                    ]
                    # 面向模型的有效视图：去掉作废文字，原始 blocks 保留用于来源与区间。
                    effective_relevant = [effective_block(b) for b in relevant]
                    request: dict[str, Any] = {
                        "stage": stage,
                        "system": self.prompts[stage],
                        "instruction": instruction,
                        "chunk": _effective_chunk(chunk, block_map).model_dump(mode="json"),
                        "document_blocks": [b.model_dump(mode="json") for b in effective_relevant],
                    }
                    if self.config.chunking.extraction_context_blocks:
                        background = neighboring_context(
                            chunk, blocks, self.config.chunking.extraction_context_blocks,
                            self.config.chunking.extraction_context_chars,
                        )
                        if background:
                            request["system"] = (
                                self.prompts[stage] + "\n\n" + self.prompts["extract_context"]
                            )
                            request["neighbor_context"] = background
                            request["neighbor_context_policy"] = CONTEXT_POLICY
                            fit_context_budget(
                                request, Extraction.model_json_schema(), self.max_context,
                                self.counter,
                            )
                    # 分块正文很短也可能因来源元数据而超限。先检查所有完整请求，
                    # 避免前面的分块已消耗模型额度，后面的分块才发现无法发送。
                    self._check_budget(
                        {**request, "output_schema": Extraction.model_json_schema()}
                    )
                    extraction_requests.append((chunk, relevant, request))
                extracted = await self._extract_concurrently(extraction_requests, run_id)
                result = reconcile(extracted)
            self.snapshot(run_id, "extract", result.model_dump(mode="json"))
            stage = "index"
            self._stage(run_id, stage)
            retrieval_evidence, dropped_slices = self._retrieval_evidence(
                result.evidence,
                blocks,
                chunks,
                max_input_bytes=self.config.models.embedding.input_byte_limit,
            )
            if dropped_slices:
                issues.append(
                    issue(
                        "gap",
                        f"{len(dropped_slices)} 条原文切片超出 embedding 单条字节预算，"
                        "未进入检索索引；这些区域的补充召回受限，请核对是否由规则证据覆盖",
                        dropped_slices,
                        severity="info",
                    )
                )
            await self.retriever.prepare(doc.content_hash, retrieval_evidence)
            for collection, values, key in (
                ("evidence_spans", result.evidence, "evidence_id"),
                ("features", result.features, "feature_id"),
                ("requirement_rules", result.rules, "rule_id"),
                ("business_entities", result.entities, "entity_id"),
            ):
                save_objects(self.repository, collection, values, key)
            issues.extend(result.issues)
            if self.config.planning.feature_catalog_report:
                issues.extend(catalog_feature_candidates(result.features))
            if self.config.planning.gap_check_rounds > 0:
                bundle = await self.retriever.retrieve(instruction, set())
                candidates = [
                    m.EvidenceSpan.model_validate(item)
                    for item in bundle["supplemental_evidence"]
                ]
                issues.extend(gap_report(result.rules, candidates))
            stage = "plan_points"
            self._stage(run_id, stage)
            blocked_rules = error_rule_ids(result.issues, result.rules)
            evidence_by_id = {item.evidence_id: item for item in result.evidence}
            features_by_id = {feature.feature_id: feature for feature in result.features}
            ui_rule_ids: set[str] = set()
            if self.config.planning.ui_display_policy == "merge":
                ui_rule_ids = {
                    rule.rule_id
                    for rule in result.rules
                    if is_ui_display_rule(rule, evidence_by_id)
                }
            planning_rules = [
                rule
                for rule in result.rules
                if rule.rule_id not in blocked_rules and rule.rule_id not in ui_rule_ids
            ]
            points = plan_points(planning_rules, self.config.planning.methods)
            if self.config.planning.combination_matrix:
                points.extend(
                    combination_points(planning_rules, self.config.planning.methods)
                )
            if ui_rule_ids:
                ui_points, ui_issues = plan_ui_display_summary(
                    result.rules,
                    {feature.feature_id: feature for feature in result.features},
                    evidence_by_id,
                )
                points.extend(ui_points)
                issues.extend(ui_issues)
            missing_boundary = [
                rule.rule_id
                for rule in planning_rules
                if rule.kind == "field_constraint"
                and not boundary_values(rule)
                and not qualitative_boundary_values(rule)
            ]
            if missing_boundary:
                issues.append(
                    issue(
                        "gap",
                        f"{len(missing_boundary)} 条字段约束没有可计算边界，未生成边界测试点",
                        missing_boundary,
                        severity="info",
                    )
                )

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
                    build_plan_query(instruction, rule_batch, features_by_id),
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
            if self.config.generation.batch_by_feature:
                rule_batches = group_by_feature(planning_rules, lambda r: r.feature_id, size)
            else:
                rule_batches = [
                    planning_rules[offset : offset + size]
                    for offset in range(0, len(planning_rules), size)
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
            points, duplicate_issues = deduplicate_points(points)
            issues.extend(duplicate_issues)
            rules_by_id = {rule.rule_id: rule for rule in result.rules}
            features_by_id = {feature.feature_id: feature for feature in result.features}
            evidence_by_id = {evidence.evidence_id: evidence for evidence in result.evidence}
            points, display_issues = merge_ui_display_points(
                points,
                rules_by_id,
                features_by_id,
                evidence_by_id,
                policy=self.config.planning.ui_display_policy,
            )
            issues.extend(display_issues)
            points, vision_issues = apply_vision_policy(
                points,
                rules_by_id,
                features_by_id,
                evidence_by_id,
                policy=self.config.planning.vision_policy,
            )
            issues.extend(vision_issues)
            if not points:
                issues.append(issue("gap", "没有得到测试点，请检查解析和模型输出"))
            save_objects(self.repository, "test_points", points, "point_id")
            tree = self._tree(
                run_id, doc.content_hash, result, points, blocks,
                layout=self.config.export.point_tree_layout,
            )
            self.repository.save("point_trees", tree.tree_id, tree)
            self.snapshot(
                run_id,
                "plan_points",
                {"tree_id": tree.tree_id, "points": [p.model_dump(mode="json") for p in points]},
            )
            issues = dedupe_issues(issues)
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
            status = _final_status(issues) if publish else "running"
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
                key: {
                    "version": self.prompt_versions.get(key, "v1"),
                    "fingerprint": fingerprint(value),
                }
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

    # 兼容原有类级入口，实际实现为独立纯函数。
    _namespace = staticmethod(namespace_extraction)

    # 兼容原有类级入口，实际实现为独立纯函数。
    _tree = staticmethod(build_point_tree)

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
            blocks = [m.DocumentBlock.model_validate(b) for b in parsed["blocks"]]
            issues = [m.Issue.model_validate(i) for i in self.read(run.run_id, "issues")["items"]]
            retrieval_evidence, dropped_slices = self._retrieval_evidence(
                extracted.evidence,
                blocks,
                [m.Chunk.model_validate(c) for c in parsed["chunks"]],
                max_input_bytes=self.config.models.embedding.input_byte_limit,
            )
            if dropped_slices:
                issues.append(
                    issue(
                        "gap",
                        f"{len(dropped_slices)} 条原文切片超出 embedding 单条字节预算，"
                        "未进入检索索引；这些区域的补充召回受限，请核对是否由规则证据覆盖",
                        dropped_slices,
                        severity="info",
                    )
                )
            await self.retriever.prepare(tree.document_fingerprint, retrieval_evidence)
            stage = "generate"
            self._stage(run.run_id, stage)
            cases: list[m.TestCase] = []
            batch_size = self.config.generation.batch_point_limit
            if self.config.generation.batch_by_feature:
                rule_feature = {r.rule_id: r.feature_id for r in extracted.rules}
                point_batches = group_by_feature(
                    points,
                    lambda p: tuple(sorted(rule_feature[rid] for rid in p.rule_ids)),
                    batch_size,
                )
            else:
                point_batches = [
                    points[offset : offset + batch_size]
                    for offset in range(0, len(points), batch_size)
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
            validated_cases, links, validation = validate_cases(
                cases, points, extracted.rules, extracted.evidence
            )
            covered = {link.point_id for link in links}
            missing = [point for point in points if point.point_id not in covered]
            repaired_cases: list[m.TestCase] = []
            for point in missing:
                try:
                    repaired = await self._generate_batch(
                        request.instruction,
                        [point],
                        extracted,
                        coverage_repair=True,
                    )
                except Exception as exc:
                    issues.append(
                        issue(
                            "coverage",
                            f"补齐未覆盖测试点失败：{type(exc).__name__}",
                            [point.point_id],
                        )
                    )
                    continue
                for item in repaired.cases:
                    item.case_id = new_id("case")
                if repaired.cases:
                    issues.append(
                        issue(
                            "coverage",
                            "已对未覆盖测试点发起定向补生成",
                            [point.point_id],
                            severity="info",
                        )
                    )
                repaired_cases.extend(repaired.cases)
                issues.extend(repaired.issues)
            if repaired_cases:
                validated_cases, links, validation = validate_cases(
                    [*cases, *repaired_cases], points, extracted.rules, extracted.evidence
                )
            cases = validated_cases
            issues.extend(validation)
            block_map = {block.block_id: block for block in blocks}
            document_stem = Path(str(parsed.get("document", {}).get("name", ""))).stem
            assign_module_paths(
                cases,
                points,
                extracted,
                block_map=block_map,
                document_stem=document_stem,
            )
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
            issues = dedupe_issues(issues)
            save_objects(self.repository, "issues", issues, "issue_id")
            rule_map = {rule.rule_id: rule for rule in extracted.rules}
            payload = {
                "schema_version": "1.0",
                "run_id": run.run_id,
                "mode": "rag",
                "cases": [c.model_dump(mode="json") for c in cases],
                "points": [
                    {
                        **point.model_dump(mode="json"),
                        "category": point_category(point, rule_map),
                    }
                    for point in sort_points(baseline, extracted)
                ],
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
            # XMind 展示全部测试点（含待澄清）；Excel 仍是全量用例视图。
            directory_tree = tree
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
            self._save_run(run, _final_status(issues), started)
            return {
                "run_id": run.run_id,
                "directory": str(target),
                "xmind": str(target / xmind_name),
                "excel": str(target / excel_name),
                "cases": len(cases),
                "issues": len(issues),
                "points_total": len(baseline),
                "points_ready": len(points),
                "points_with_cases": len({pid for case in cases for pid in case.point_ids}),
            }
        except BaseException as exc:
            self.snapshot(run.run_id, "failure", {"stage": stage, "error_type": type(exc).__name__})
            self._save_run(
                run, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", started
            )
            raise

    @staticmethod
    def _retrieval_evidence(
        evidence: list[m.EvidenceSpan],
        blocks: list[m.DocumentBlock],
        chunks: list[m.Chunk],
        *,
        max_input_bytes: int | None = None,
    ) -> tuple[list[m.EvidenceSpan], list[str]]:
        """原文片段也入检索库，避免提炼遗漏限制召回；不把片段自动登记为规则。

        max_input_bytes 用于约束检索索引单条入量：模型提炼证据（rule 直接引用）必须保留，
        原文切片是补充召回的引导，超过 embedding 单条预算时跳过并返回其 evidence_id，
        避免整次索引构建因单条超限失败；被跳过区域仍可由规则证据直接提供。
        """
        result = {e.evidence_id: e for e in evidence}
        block_map = {b.block_id: b for b in blocks}
        dropped: list[str] = []
        for chunk in chunks:
            for source in chunk.sources:
                block = block_map[source.block_id]
                quote = effective_slice(
                    block.content,
                    source.start_char,
                    source.end_char,
                    block.deprecated_ranges,
                )
                if not quote:
                    # 该来源区间整段作废，不入索引，避免旧索引返回作废文字。
                    continue
                key = "source_" + fingerprint(source.model_dump(mode="json"))
                if max_input_bytes is not None and len(quote.encode("utf-8")) > max_input_bytes:
                    # 单条超出 embedding 输入预算的切片不进索引，否则整次 prepare 失败。
                    dropped.append(key)
                    continue
                result[key] = m.EvidenceSpan(
                    evidence_id=key,
                    block_id=block.block_id,
                    locator=block.locator,
                    quote=quote,
                    origin=block.origin,
                    processing_model=block.processing_model,
                    processing_version=block.processing_version,
                )
        return list(result.values()), dropped

    # 以下私有方法保留为编排层入口，委托给 ExtractionEngine/CaseGenerator；测试与旧调用直接使用。
    def _check_budget(self, request: dict[str, Any]) -> None:
        self.engine._check_budget(request)

    async def _model(self, request: dict[str, Any], schema: type[T]) -> ModelResult:
        return await self.engine._model(request, schema)

    async def _extract_concurrently(
        self,
        requests: list[tuple[m.Chunk, list[m.DocumentBlock], dict[str, Any]]],
        run_id: str,
    ) -> list[Extraction]:
        return await self.engine._extract_concurrently(requests, run_id)

    async def _batches(
        self, requests: list[B], operation: Callable[[B], Awaitable[T]], stage: str
    ) -> list[T]:
        return await self.engine._batches(requests, operation, stage)

    async def _extract_chunk(
        self, chunk: m.Chunk, blocks: list[m.DocumentBlock], request: dict[str, Any], run_id: str
    ) -> Extraction:
        return await self.engine._extract_chunk(chunk, blocks, request, run_id)

    async def _supplement(
        self,
        request: dict[str, Any],
        direct_ids: set[str],
        query: str,
        schema: type[T],
    ) -> None:
        await self.generator._supplement(request, direct_ids, query, schema)

    async def _generate_batch(
        self,
        instruction: str,
        points: list[m.TestPoint],
        extracted: Extraction,
        *,
        coverage_repair: bool = False,
    ) -> CaseBatch:
        return await self.generator._generate_batch(
            instruction, points, extracted, coverage_repair=coverage_repair
        )
