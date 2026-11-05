"""在相同原文、计数器与已确认标注上做预算网格对照；不选择所谓最优参数。"""

from itertools import product
from time import perf_counter
from typing import Any

from tc_rag.application.services.chunk_coverage import coverage_diagnostics
from tc_rag.application.services.chunk_gold import ChunkGold, evaluate_boundaries, validate_gold
from tc_rag.application.services.chunk_stats import chunk_statistics
from tc_rag.application.services.chunking import TokenCounter, structural_chunks
from tc_rag.domain.models import DocumentBlock


def budget_pairs(
    targets: list[int], overlaps: list[int], minimum: int,
) -> list[tuple[int, int]]:
    """全部组合先预检；非法组合失败，不偷偷跳过使实验矩阵不完整。"""
    if type(minimum) is not int or any(type(x) is not int for x in [*targets, *overlaps]):
        raise ValueError("预算参数必须为整数")
    targets, overlaps = sorted(set(targets)), sorted(set(overlaps))
    if not targets or not overlaps or len(targets) * len(overlaps) > 32:
        raise ValueError("预算对照需要 1～32 组不同组合")
    pairs = list(product(targets, overlaps))
    if any(type(t) is not int or type(o) is not int or not 0 <= o < t or not 0 <= minimum < t
           for t, o in pairs):
        raise ValueError("每组 target 必须为正，overlap/min_tokens 均应非负且小于 target")
    return pairs


def budget_experiment(
    run_id: str, blocks: list[DocumentBlock], counter: TokenCounter,
    pairs: list[tuple[int, int]], options: dict[str, Any], gold: ChunkGold | None = None,
) -> dict[str, Any]:
    if gold is not None:
        validate_gold(gold, run_id, blocks)
    # 入口也验证，防止其他调用者绕过 CLI 的组合数和预算限制。
    checked = budget_pairs([t for t, _ in pairs], [o for _, o in pairs], options["min_tokens"])
    if set(checked) != set(pairs):
        raise ValueError("预算组合必须构成完整网格")
    variants = []
    for target, overlap in checked:
        started = perf_counter()
        result = structural_chunks(blocks, target, overlap, counter, **options)
        elapsed = (perf_counter() - started) * 1000
        repeated = structural_chunks(blocks, target, overlap, counter, **options)
        stats = chunk_statistics(blocks, result.chunks, result.issues, target, counter)
        variants.append({
            "variant_id": f"{options['strategy']}_t{target}_o{overlap}",
            "settings": {"target_tokens": target, "overlap_tokens": overlap, **options},
            "statistics": stats,
            "coverage": coverage_diagnostics(blocks, result.chunks, result.issues, target),
            "gold": evaluate_boundaries(gold, run_id, blocks, result.chunks),
            "stable_chunk_ids": result.chunks == repeated.chunks,
            "chunk_text_estimated_tokens": sum(c.token_estimate for c in result.chunks),
            "local_chunking_ms": elapsed,
            "chunks": [c.model_dump(mode="json") for c in result.chunks],
            "issues": [i.model_dump(mode="json") for i in result.issues],
        })
    return {
        "run_id": run_id, "model_calls": 0, "variants": variants,
        "selected_variant": None,
        "note": "相同原文的离线分块对照；token 仅为分块文字估算，耗时仅为本地分块，"
                "不代表完整请求、真实模型费用/耗时或业务质量。未自动选择最优参数。",
    }
