"""采集固定问题的 embedding；默认预览，只有 --live 允许请求。"""

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.core.config import load_config
from tc_rag.domain.identity import fingerprint


def validate_batch(batch, space, count):
    if batch.space != space or len(batch.vectors) != count:
        raise ValueError("返回向量空间或数量不一致")
    matrix = np.asarray(batch.vectors, dtype=np.float32)
    if matrix.shape != (count, space.dimension) or not np.isfinite(matrix).all():
        raise ValueError("返回向量维度或数值非法")
    if space.normalize and np.any(np.linalg.norm(matrix, axis=1) == 0):
        raise ValueError("返回零向量")


async def collect(bundle, output, config, root, live=False):
    if output.exists():
        raise ValueError("输出已存在，拒绝覆盖")
    if not (root / "data.sqlite").is_file():
        raise ValueError("知识库数据库不存在")
    manifest = json.loads((bundle / "manifest.partial.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise ValueError("材料指纹不一致：" + name)
    snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))["snapshot"]
    questions = json.loads((bundle / "questions.evidence_draft.json").read_text(encoding="utf-8"))
    ids = [q["query_id"] for q in questions]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("问题 ID 为空或重复")
    texts = [q["question"] for q in questions]
    if any(not isinstance(t, str) or not t.strip() for t in texts):
        raise ValueError("问题文字为空")
    settings = load_config(config)
    space = EmbeddingSpace(
        provider=settings.models.embedding.base_url or "openai",
        model=settings.models.embedding.model,
        dimension=settings.retrieval.embedding_dimension,
        normalize=settings.retrieval.normalize,
    )
    if fingerprint(asdict(space)) != snapshot["embedding_space_id"]:
        raise ValueError("当前 embedding 配置与冻结快照空间不同，禁止采集")
    print(f"[预检] {len(texts)} 题，维度 {space.dimension}；只采集查询 embedding。")
    if not live:
        print("[预览] 未调用模型、未写缓存。实际执行需 --live；供应商可能分批或重试。")
        return
    from tc_rag.bootstrap import build_kb_ingestion

    # 强制缓存复用；不创建 Vision 服务，不运行文档解析、入库或检索。
    settings.runtime.cache_enabled = True
    service, provider = build_kb_ingestion(settings, root)
    try:
        current = service.repository.snapshot(snapshot["kb_id"], snapshot["snapshot_id"])
        if current is None or current.snapshot.model_dump() != snapshot:
            raise ValueError("冻结快照与数据库不一致")
        batch = await service.embedder.embed_batch(texts, space)
        validate_batch(batch, space, len(texts))
        payload = {
            "snapshot_id": snapshot["snapshot_id"], "space": asdict(space),
            "queries": [{"query_id": q["query_id"], "question": q["question"], "vector": v}
                        for q, v in zip(questions, batch.vectors)],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        # 独占创建防止并发运行覆盖已采集文件。失败时已有缓存可供重试。
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        print(f"[完成] 已保存 {len(texts)} 条查询向量：{output}")
    finally:
        await provider.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    parser.add_argument("--root", type=Path, default=Path("workdir/kb/runtime"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    asyncio.run(collect(args.bundle, args.output, args.config, args.root, args.live))
