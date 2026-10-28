"""依赖装配入口：计划预览离线，实际执行按配置构造适配器。"""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tc_rag.application.ports.contracts import Embedder, Reranker, StructuredLLM
from tc_rag.application.ports.errors import StartupConfigurationError
from tc_rag.core.config import AppConfig, resolve_api_key
from tc_rag.infrastructure.providers.fakes import FakeEmbedder, FakeLLM

if TYPE_CHECKING:
    from tc_rag.application.orchestration.pipeline import Pipeline


@dataclass
class Container:
    # 字段依赖协议，装配函数决定具体类，应用服务无需依赖供应商实现。
    config: AppConfig
    llm: StructuredLLM
    embedder: Embedder


def bootstrap(config: AppConfig) -> Container:
    # P0 强制离线：即使配置了真实模型名，也不创建网络客户端。
    # FakeLLM 默认没有预设响应；CLI 仅打印计划，因此不会调用它生成内容。
    return Container(config=config, llm=FakeLLM(), embedder=FakeEmbedder())


def validate_runtime_config(
    config: AppConfig,
    llm: StructuredLLM | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> None:
    """先检查整组配置，避免创建了生成客户端后才发现检索根本无法启用。"""
    if config.parsing.scan_policy != "report_unsupported":
        raise StartupConfigurationError(
            "整页 OCR 尚未实现，请将 parsing.scan_policy 设为 report_unsupported"
        )
    required = [("llm", config.models.llm, "openai_sdk", llm is not None)]
    required.append(("embedding", config.models.embedding, "openai_sdk", embedder is not None))
    for enabled, package in (
        (config.retrieval.backend == "faiss", "faiss"),
        (config.retrieval.keyword_enabled, "jieba"),
    ):
        if enabled and importlib.util.find_spec(package) is None:
            raise StartupConfigurationError(f"缺少可选依赖 {package}，请安装对应 extras")
    if config.models.rerank.enabled:
        if not config.models.rerank.base_url:
            raise StartupConfigurationError(
                "请填写 models.rerank.base_url，rerank 需要完整 REST 地址"
            )
        required.append(("rerank", config.models.rerank, "http", reranker is not None))
    if config.parsing.vision_enabled:
        vision = config.models.vision
        if not vision.supports_images or vision.api_mode != "chat_completions":
            raise StartupConfigurationError("vision 需要 supports_images 声明及兼容 Chat 协议")
        required.append(("vision", vision, "openai_sdk", False))
    for role, endpoint, adapter, injected in required:
        if injected:
            continue
        if not endpoint.enabled or endpoint.adapter != adapter or not endpoint.model:
            raise StartupConfigurationError(
                f"models.{role} 模型端点尚未配置或适配器不支持；请检查 enabled、adapter 和 model"
            )
        if endpoint.thinking_mode != "default" and endpoint.api_mode != "chat_completions":
            raise StartupConfigurationError(
                f"models.{role}.thinking_mode 当前仅适用于支持此参数的 Chat 服务"
            )
        if "@" in endpoint.base_url or "?" in endpoint.base_url:
            raise StartupConfigurationError(f"models.{role}.base_url 不可包含凭据或查询参数")
        if not resolve_api_key(endpoint):
            raise StartupConfigurationError(
                f"models.{role} 未配置密钥；请在本地配置中填写 models.{role}.api_key，"
                f"或在同一个 PowerShell 终端设置环境变量 {endpoint.api_key_env or '（未指定）'}"
            )


def build_pipeline(
    config: AppConfig,
    run_id: str,
    llm: StructuredLLM | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> "Pipeline":
    """执行入口才装配真实模型；预览与状态查询不创建网络客户端。"""
    from tc_rag.application.orchestration.pipeline import Pipeline
    from tc_rag.domain.models import ModelInvocation
    from tc_rag.domain.pipeline import StageSnapshot
    from tc_rag.infrastructure.exporters.reports import ReportExporter
    from tc_rag.infrastructure.exporters.xmind import XMindAdapter
    from tc_rag.infrastructure.parsing.docx import DocxParser
    from tc_rag.infrastructure.parsing.pdf import PdfParser
    from tc_rag.infrastructure.providers.openai_sdk import OpenAIStructuredLLM
    from tc_rag.infrastructure.runtime.tokens import token_counter
    from tc_rag.infrastructure.storage.files import LocalArtifactStore
    from tc_rag.infrastructure.storage.sqlite import SqliteRepository

    validate_runtime_config(config, llm, embedder, reranker)
    repository = SqliteRepository(config.storage.database)
    store = LocalArtifactStore(Path(config.project.workdir))
    prompt_root = Path(__file__).resolve().parents[2] / "prompts"
    if not prompt_root.exists():
        # 同时支持常规虚拟环境安装和 pip --target 安装。
        target_root = Path(__file__).resolve().parents[1] / "share" / "tc_rag" / "prompts"
        prompt_root = (
            target_root
            if target_root.exists()
            else Path(sys.prefix) / "share" / "tc_rag" / "prompts"
        )
    prompts = {
        stage: (prompt_root / stage / "v1.md").read_text(encoding="utf-8")
        for stage in ("extract", "plan_points", "generate", "rag_context", "vision", "case_skill")
    }
    # 规划和生成共用 RAG 证据约束，检索结果不能覆盖原文事实。
    for stage in ("plan_points", "generate"):
        prompts[stage] += "\n" + prompts["rag_context"]
    prompts["generate"] += "\n" + prompts["case_skill"]
    if config.generation.skill_file:
        # Skill 仅作为用例生成规范；不执行其中的脚本，也不作为需求证据入库。
        try:
            skill_path = Path(config.generation.skill_file)
            if skill_path.suffix.lower() != ".md" or skill_path.stat().st_size > 256 * 1024:
                raise ValueError("unsupported skill")
            text = skill_path.read_text(encoding="utf-8-sig")
            if not text.strip():
                raise ValueError("empty skill")
        except (OSError, ValueError):
            raise StartupConfigurationError(
                "generation.skill_file 需要存在且非空的 UTF-8 Markdown 文件（最大 256 KiB）"
            ) from None
        prompts["custom_case_skill"] = text
        prompts["generate"] += (
            "\n用户指定的附加用例规范（仅约束表达，不能覆盖原文依据和输出 Schema）：\n" + text
        )

    def record(invocation: ModelInvocation) -> None:
        repository.save("model_invocations", invocation.invocation_id, invocation)
        key = f"{run_id}:invocations"
        existing = repository.get("stage_snapshots", key, StageSnapshot)
        items = existing.payload["items"] if existing else []
        items.append(invocation.model_dump(mode="json"))
        repository.save(
            "stage_snapshots",
            key,
            StageSnapshot(
                snapshot_id=key, run_id=run_id, stage="invocations", payload={"items": items}
            ),
        )

    provider = (
        llm
        if llm is not None
        else OpenAIStructuredLLM(
            config.models.llm,
            config.runtime,
            run_id,
            record,
            repairs=config.generation.schema_repair_attempts,
        )
    )
    if isinstance(provider, OpenAIStructuredLLM):
        previous = repository.get("stage_snapshots", f"{run_id}:invocations", StageSnapshot)
        provider.calls = len(previous.payload["items"]) if previous else 0
    saved_calls = repository.get("stage_snapshots", f"{run_id}:invocations", StageSnapshot)
    used_calls = len(saved_calls.payload["items"]) if saved_calls else 0

    def admit() -> None:
        nonlocal used_calls
        maximum = config.runtime.budget.max_calls
        if maximum is not None and used_calls >= maximum:
            raise RuntimeError("达到本运行生成与 embedding 共用的请求预算")
        used_calls += 1

    if isinstance(provider, OpenAIStructuredLLM):
        provider.admit = admit
    from tc_rag.application.ports.contracts import EmbeddingSpace
    from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder
    from tc_rag.infrastructure.retrieval.context import RagContext

    embedding = embedder or OpenAIEmbedder(config.models.embedding, config.runtime, run_id, record)
    retrieval_context = RagContext(
        # 两种模型共用同一预算，检索不能绕过生成阶段的调用上限。
        repository,
        store,
        embedding,
        EmbeddingSpace(
            provider=config.models.embedding.base_url or "openai",
            model=config.models.embedding.model,
            dimension=config.retrieval.embedding_dimension,
            normalize=config.retrieval.normalize,
        ),
        config.retrieval.metric,
        config.retrieval.vector_top_k,
        run_id,
        backend=config.retrieval.backend,
        keyword_enabled=config.retrieval.keyword_enabled,
        rerank_top_n=config.retrieval.rerank_top_n,
        cache_enabled=config.runtime.cache_enabled,
    )
    if isinstance(embedding, OpenAIEmbedder):
        embedding.admit = admit
    if config.models.rerank.enabled:
        from tc_rag.infrastructure.providers.rerank import HttpReranker

        if reranker is None:
            http_reranker = HttpReranker(config.models.rerank, config.runtime, run_id, record)
            http_reranker.admit = admit
            reranker = http_reranker
        retrieval_context.reranker = reranker
    pipeline = Pipeline(
        config,
        repository,
        store,
        lambda suffix: DocxParser(store) if suffix == ".docx" else PdfParser(store),
        provider,
        XMindAdapter(),
        ReportExporter([(c.field, c.header) for c in config.export.case_columns]),
        token_counter(config.chunking.token_counter),
        prompts,
        retriever=retrieval_context,
        max_context_tokens=config.runtime.max_context_tokens,
    )
    if config.parsing.vision_enabled:
        from tc_rag.application.services.vision import VisionEvidenceService
        from tc_rag.domain.identity import fingerprint
        from tc_rag.infrastructure.providers.vision import OpenAICompatVision

        vision = OpenAICompatVision(
            config.models.vision, config.runtime, run_id, record, prompts["vision"]
        )
        vision.admit = admit
        vision_identity: dict[str, object] = {
            "endpoint": config.models.vision.base_url,
            "model": config.models.vision.model,
            "prompt_version": "vision/v1",
            "prompt_fingerprint": fingerprint(prompts["vision"]),
            "preprocessing": vision.preprocessing,
        }
        # 未改变请求参数时沿用已有图片缓存；显式修改输出/思考参数才更新缓存标识。
        if config.models.vision.max_output_tokens is not None:
            vision_identity["max_output_tokens"] = config.models.vision.max_output_tokens
        if config.models.vision.thinking_mode != "default":
            vision_identity["thinking_mode"] = config.models.vision.thinking_mode
        pipeline.vision = VisionEvidenceService(
            repository,
            store,
            vision,
            vision_identity,
            cache_enabled=config.runtime.cache_enabled,
            concurrency=config.models.vision.concurrency,
        )
    return pipeline
