"""补采未归一化原始向量，供 E1/E2 的「未归一化」组使用。

历史 embedding 缓存是在 normalize=true 的空间里写入的，反推不出原始模长。
本脚本以 normalize=false 的空间重新请求同一批文本，结果单独存放，不覆盖材料包。

    python eval/resample_raw_vectors.py --dataset <材料包> --output <材料包>/raw
"""

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.core.config import load_config
from tc_rag.domain.models import ModelInvocation
from tc_rag.infrastructure.providers.embedding import OpenAIEmbedder


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


async def resample(dataset: Path, output: Path, config_path: Path) -> dict[str, Any]:
    corpus = _load(dataset / "corpus.json")
    queries = _load(dataset / "queries.json")
    texts = [str(item["quote"]) for item in corpus] + [str(item["text"]) for item in queries]
    if any(not text.strip() for text in texts):
        raise ValueError("存在空文本，拒绝补采")

    config = load_config(config_path)
    endpoint = config.models.embedding
    space = EmbeddingSpace(
        provider=endpoint.base_url or "openai",
        model=endpoint.model,
        dimension=config.retrieval.embedding_dimension,
        normalize=False,
        template_version="v1",
    )
    invocations: list[ModelInvocation] = []
    embedder = OpenAIEmbedder(endpoint, config.runtime, "raw-resample", invocations.append)
    batches = -(-len(texts) // max(1, endpoint.batch_size))
    print(
        f"待补采文本 {len(texts)} 条（语料 {len(corpus)} + 查询 {len(queries)}），约 {batches} 批"
    )
    started = perf_counter()
    try:
        batch = await embedder.embed_batch(texts, space)
    finally:
        await embedder.close()
    elapsed = perf_counter() - started

    vectors = np.asarray(batch.vectors, dtype=np.float64)
    if vectors.shape != (len(texts), space.dimension):
        raise ValueError(f"返回矩阵形状 {vectors.shape} 与空间维度 {space.dimension} 不符")
    if not np.all(np.isfinite(vectors)):
        raise ValueError("返回向量包含非有限数")
    zero = int(np.sum(np.linalg.norm(vectors, axis=1) == 0))

    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "corpus_raw.npy", vectors[: len(corpus)].astype(np.float32))
    np.save(output / "query_raw.npy", vectors[len(corpus) :])
    usage = {
        "input_tokens": sum(item.usage.input_tokens or 0 for item in invocations),
        "calls": len(invocations),
    }
    manifest = {
        "vector_origin": "resampled_raw_provider_output",
        "normalize": False,
        "model": space.model,
        "dimension": space.dimension,
        "template_version": space.template_version,
        "texts": len(texts),
        "corpus_rows": len(corpus),
        "query_rows": len(queries),
        "corpus_dtype": "float32",
        "query_dtype": "float64",
        "zero_vectors": zero,
        "usage": usage,
        "elapsed_seconds": round(elapsed, 3),
        "note": "与历史归一化缓存分开保存；不得与缓存向量混用",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="补采未归一化原始向量（会调用真实 embedding）")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    args = parser.parse_args()
    if (args.output / "corpus_raw.npy").exists():
        raise SystemExit("输出已存在，拒绝覆盖；确需重采请先手动移除")
    manifest = asyncio.run(resample(args.dataset, args.output, args.config))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
