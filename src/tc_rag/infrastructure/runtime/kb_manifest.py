"""按白名单逐项记录非凭据参数，避免持久化整个模型配置。"""

import hashlib
import platform
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from tc_rag.core.config import AppConfig


def runtime_manifest(config: AppConfig, prompt_root: Path) -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    models = {}
    for name in ("llm", "embedding", "rerank", "vision", "vision_review"):
        endpoint = getattr(config.models, name)
        models[name] = {
            "model": endpoint.model, "adapter": endpoint.adapter,
            "enabled": endpoint.enabled, "concurrency": endpoint.concurrency,
            "batch_size": endpoint.batch_size,
            "api_mode": endpoint.api_mode, "response_format": endpoint.response_format,
            "thinking_mode": endpoint.thinking_mode,
            "max_output_tokens": endpoint.max_output_tokens,
            "endpoint_sha256": hashlib.sha256(endpoint.base_url.encode("utf-8")).hexdigest(),
        }
    prompts = {}
    libraries: dict[str, str | None] = {}
    for package in ("numpy", "pydantic", "python-docx", "pymupdf", "faiss-cpu", "jieba"):
        try:
            libraries[package] = version(package)
        except PackageNotFoundError:
            libraries[package] = None
    for stage in ("kb_qa", "vision", "vision_verify"):
        for path in sorted((prompt_root / stage).glob("*.md")):
            prompts[f"{stage}/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "started_utc": datetime.now(UTC).isoformat(), "python": platform.python_version(),
        "platform": platform.platform(), "source_sha256": digest.hexdigest(),
        "libraries": libraries,
        "prompt_sha256": prompts, "models": models,
        "retrieval": {
            "backend": config.retrieval.backend, "metric": config.retrieval.metric,
            "normalize": config.retrieval.normalize,
            "dimension": config.retrieval.embedding_dimension,
            "vector_top_k": config.retrieval.vector_top_k,
            "mode": config.retrieval.mode,
            "rerank_top_n": config.retrieval.rerank_top_n,
        },
        "budget": {"max_calls": config.runtime.budget.max_calls,
                   "max_context_tokens": config.runtime.max_context_tokens},
        "chunking": {
            "strategy": config.chunking.strategy, "target_tokens": config.chunking.target_tokens,
            "overlap_tokens": config.chunking.overlap_tokens,
            "token_counter": config.chunking.token_counter,
            "attach_headings": config.chunking.attach_headings,
            "min_tokens": config.chunking.min_tokens,
            "split_long_paragraphs": config.chunking.split_long_paragraphs,
        },
        "parsing": {
            "vision_enabled": config.parsing.vision_enabled,
            "infer_docx_headings": config.parsing.infer_docx_headings,
            "docx_extended": config.parsing.docx_extended,
            "pdf_running_text": config.parsing.pdf_running_text,
            "vision": {
                name: getattr(config.parsing.vision, name) for name in (
                    "max_edge", "tile_edge", "tile_overlap", "max_tiles", "detail",
                    "context_blocks", "context_chars", "verify", "verify_failure", "min_edge",
                    "auto_fact_threshold",
                )
            },
        },
        "runtime": {
            "cache_enabled": config.runtime.cache_enabled,
            "max_attempts": config.runtime.max_attempts,
            "request_timeout_seconds": config.runtime.request_timeout_seconds,
            "vision_min_interval_seconds": config.runtime.vision_min_interval_seconds,
        },
    }
