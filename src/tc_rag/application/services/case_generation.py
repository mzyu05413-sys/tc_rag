"""用例生成：批次模型调用、检索补充证据与上下文裁剪。"""

from time import perf_counter
from typing import Any, Protocol

from tc_rag.application.ports.contracts import (
    ModelResult,
    Repository,
    RetrievalContext,
    T,
)
from tc_rag.application.ports.errors import OutputTruncatedError, WorkflowError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.services.design import build_case_query
from tc_rag.application.services.generation_context import evidence_fingerprint
from tc_rag.core.config import AppConfig
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import (
    CaseBatch,
    Extraction,
    StageSnapshot,
    case_batch_schema,
)
from tc_rag.domain.rag import RagRecord


class CaseRuntime(Protocol):
    """用例生成所需的编排运行时视图；由 Pipeline 实现，读取运行状态与外部适配器。"""

    config: AppConfig
    prompts: dict[str, str]
    repository: Repository
    retriever: RetrievalContext
    progress: ProgressReporter | None
    max_context: int
    active_run_id: str

    def snapshot(self, run_id: str, stage: str, payload: dict[str, Any]) -> None: ...

    def _check_budget(self, request: dict[str, Any]) -> None: ...

    async def _model(self, request: dict[str, Any], schema: type[T]) -> ModelResult: ...

    async def _supplement(
        self, request: dict[str, Any], direct_ids: set[str], query: str, schema: type[T]
    ) -> None: ...

    async def _generate_batch(
        self,
        instruction: str,
        points: list[m.TestPoint],
        extracted: Extraction,
        *,
        coverage_repair: bool = False,
    ) -> CaseBatch: ...


class CaseGenerator:
    """封装测试用例的批次生成与检索补充；运行生命周期仍由编排层持有。"""

    def __init__(self, runtime: CaseRuntime) -> None:
        self._rt = runtime

    async def _supplement(
        self,
        request: dict[str, Any],
        direct_ids: set[str],
        query: str,
        schema: type[T],
    ) -> None:
        started = perf_counter()
        bundle = await self._rt.retriever.retrieve(query, direct_ids)
        # 仅上报计数和固定状态，不向终端暴露查询、证据或供应商响应正文。
        report(
            self._rt.progress,
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
                self._rt._check_budget(
                    {**request, "output_schema": schema.model_json_schema()}
                )
                break
            except ValueError:
                omitted.append(supplement.pop()["evidence_id"])
        request["supplemental_evidence"] = supplement
        bundle["adopted_ids"] = [e["evidence_id"] for e in supplement]
        bundle["omitted_ids"] = omitted
        bundle["generation"] = {
            "stage": request.get("stage"),
            "schema": schema.__name__,
            "max_context_tokens": self._rt.max_context,
            "model": self._rt.config.models.llm.model,
            "evidence_fingerprint": evidence_fingerprint(request),
            "query": query,
        }
        bundle["timings_ms"]["context_build_ms"] += (perf_counter() - started) * 1000 - sum(
            bundle["timings_ms"].values()
        )
        key = f"{self._rt.active_run_id}:retrieval"
        previous = self._rt.repository.get("stage_snapshots", key, StageSnapshot)
        bundles = previous.payload["bundles"] if previous else []
        bundles.append(bundle)
        self._rt.snapshot(self._rt.active_run_id, "retrieval", {"bundles": bundles})
        # 保存最终采用/省略结果，不能只留下裁剪前的原始命中。
        key = "context_" + fingerprint([self._rt.active_run_id, query, sorted(direct_ids)])
        self._rt.repository.save(
            "context_bundles", key, RagRecord(record_id=key, kind="context", payload=bundle)
        )

    async def _generate_batch(
        self,
        instruction: str,
        points: list[m.TestPoint],
        extracted: Extraction,
        *,
        coverage_repair: bool = False,
    ) -> CaseBatch:
        rule_ids = {r for p in points for r in p.rule_ids}
        # 在模型输出和缓存写入前校验当前批次 ID，复用适配器的有界修复而不放宽引用。
        output_schema = case_batch_schema(
            [point.point_id for point in points],
            self._rt.config.generation.max_cases_per_point,
        )
        rules = [r for r in extracted.rules if r.rule_id in rule_ids]
        rules_by_id = {r.rule_id: r for r in rules}
        features_by_id = {f.feature_id: f for f in extracted.features}
        # 核心证据只取本批测试点引用规则的证据；功能背景进入查询构造（build_case_query），
        # 不再把同功能全部规则塞入上下文。同功能的其他条件与例外由检索补充证据覆盖。
        evidence_ids = {e for r in rules for e in r.evidence_ids}
        request = {
            "stage": "generate",
            "system": self._rt.prompts["generate"],
            "instruction": instruction,
            "points": [p.model_dump(mode="json") for p in points],
            "limits": {"max_cases_per_point": self._rt.config.generation.max_cases_per_point},
            "coverage_repair": coverage_repair,
            "rules": [r.model_dump(mode="json") for r in rules],
            "evidence": [
                e.model_dump(mode="json")
                for e in extracted.evidence
                if e.evidence_id in evidence_ids
            ],
        }
        try:
            self._rt._check_budget(
                {**request, "output_schema": output_schema.model_json_schema()}
            )
        except ValueError:
            if len(points) == 1:
                raise
            middle = len(points) // 2
            left = await self._rt._generate_batch(
                instruction, points[:middle], extracted, coverage_repair=coverage_repair
            )
            right = await self._rt._generate_batch(
                instruction, points[middle:], extracted, coverage_repair=coverage_repair
            )
            return CaseBatch(cases=left.cases + right.cases, issues=left.issues + right.issues)
        await self._rt._supplement(
            request,
            evidence_ids,
            build_case_query(points, rules_by_id, features_by_id),
            output_schema,
        )
        try:
            model_result = await self._rt._model(request, output_schema)
        except OutputTruncatedError:
            if len(points) == 1:
                raise
            middle = len(points) // 2
            left = await self._rt._generate_batch(
                instruction, points[:middle], extracted, coverage_repair=coverage_repair
            )
            right = await self._rt._generate_batch(
                instruction, points[middle:], extracted, coverage_repair=coverage_repair
            )
            return CaseBatch(cases=left.cases + right.cases, issues=left.issues + right.issues)
        candidate = CaseBatch.model_validate(model_result.output.model_dump(mode="json"))
        if len(candidate.cases) > len(points) * self._rt.config.generation.max_cases_per_point:
            raise WorkflowError("模型返回用例超过当前批次数量上限")
        allowed = {p.point_id for p in points}
        if any(not set(c.point_ids) <= allowed for c in candidate.cases):
            raise WorkflowError("模型生成了不属于当前批次的测试点引用")
        if any(
            sum(point_id in c.point_ids for c in candidate.cases)
            > self._rt.config.generation.max_cases_per_point
            for point_id in allowed
        ):
            raise WorkflowError("单个测试点用例数量超过配置上限")
        return candidate
