"""开发集固定40候选重排实验；默认预览，不重新召回。"""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
from time import perf_counter

from tc_rag.application.ports.contracts import RankedCandidate
from tc_rag.core.config import Retrieval, load_config


async def reorder(pool, reranker):
    candidates = pool["candidates"]
    expected = [c["chunk_id"] for c in candidates]
    if len(expected) != len(set(expected)):
        raise ValueError("候选池重复ID")
    started = perf_counter()
    ranked = await reranker.rank(pool["question"], [
        RankedCandidate(c["chunk_id"], c["text"]) for c in candidates])
    ids = [r.object_id for r in ranked]
    if len(ids) != len(expected) or set(ids) != set(expected):
        raise ValueError("重排改变候选集合")
    if any(r.score is None or not math.isfinite(r.score) for r in ranked):
        raise ValueError("重排分数非法")
    return {"query_id": pool["query_id"], "before": expected, "after": ids,
            "scores": {r.object_id: r.score for r in ranked},
            "seconds": perf_counter()-started, "status": "success"}


async def execute(
    pool_path, output, config, root, live=False, allow_holdout=False, tie_policy=None,
):
    if output.exists():
        raise ValueError("输出已存在")
    payload = json.loads(pool_path.read_text("utf-8"))
    pools = payload["questions"]
    allowed = "test" if allow_holdout else "dev"
    if not pools or any(p.get("split") != allowed for p in pools):
        raise ValueError("集合不匹配；留出补充对照需显式--allow-holdout")
    if allow_holdout and payload.get("experiment_kind") != "holdout_supplement":
        raise ValueError("缺少留出补充对照声明")
    for pool in pools:
        if not pool["candidates"] or len(pool["candidates"]) > 128:
            raise ValueError("候选池大小非法")
        if len(pool["question"]) > 4096 or any(len(c["text"]) > 4096
                                             for c in pool["candidates"]):
            raise ValueError("文本超出适配器限制；禁止静默截断")
    settings = load_config(config)
    if tie_policy is not None:
        settings.retrieval = Retrieval.model_validate({
            **settings.retrieval.model_dump(), "rerank_tie_policy": tie_policy,
        })
    if payload.get("expected_model") and settings.models.rerank.model != payload["expected_model"]:
        raise ValueError("重排模型与预登记配置不一致")
    print(f"固定池预检：{len(pools)}题；rerank启用={settings.models.rerank.enabled}")
    print(f"同分策略：{settings.retrieval.rerank_tie_policy}")
    if not live:
        print("未调用模型；--live只请求重排，不调用embedding/回答。")
        return
    from tc_rag.bootstrap import kb_session

    if not settings.models.rerank.enabled:
        raise ValueError("需先启用models.rerank并配置现有http重排适配器")
    output.mkdir(parents=True)
    async with kb_session(settings, root, with_answer=False) as (search, _):
        if search.reranker is None:
            raise ValueError("重排未装配")
        for pool in pools:
            try:
                row = await reorder(pool, search.reranker)
            except Exception as exc:
                row = {"query_id": pool["query_id"], "status": "error",
                       "error_type": type(exc).__name__}
            row["pool_sha256"] = hashlib.sha256(pool_path.read_bytes()).hexdigest()
            row["tie_policy"] = settings.retrieval.rerank_tie_policy
            with (output / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")
    (output / "complete.json").write_text(json.dumps({
        "status": "finished_errors_require_separate_reporting",
        "model": settings.models.rerank.model,
        "tie_policy": settings.retrieval.rerank_tie_policy,
        "input_policy": payload.get("input_policy", "original_pool_text"),
        "pool_sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    parser.add_argument("--root", type=Path, default=Path("workdir/kb/runtime"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-holdout", action="store_true")
    parser.add_argument("--tie-policy", choices=("provider_order", "input_order"))
    args = parser.parse_args()
    asyncio.run(execute(
        args.pool, args.output, args.config, args.root, args.live, args.allow_holdout,
        args.tie_policy))
