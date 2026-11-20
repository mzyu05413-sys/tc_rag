"""检索补充证据去重：按 (block_id, quote) 精确折叠同源重复，记录覆盖关系。

EvidenceSpan 目前不携带字符区间（区间只在 ChunkSource 中），因此 v1 以
(block_id, quote) 作为同源判据，处理“模型提炼引文与原文切片逐字相同”这一明确重复；
部分重叠/包含关系依赖后续有效文本视图（A9）的区间字段，不在本函数内推测。
"""

from collections.abc import Sequence
from typing import Any


def dedupe_sources(
    evidence: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """按 (block_id, quote) 精确去重，保留首次出现，返回 (去重列表, 覆盖记录)。

    覆盖记录为 kept_evidence_id -> [被折叠的 evidence_id ...]，只报告关系、不改写证据。
    """
    seen: dict[tuple[str, str], str] = {}
    result: list[dict[str, Any]] = []
    coverage: dict[str, list[str]] = {}
    for item in evidence:
        key = (item["block_id"], item["quote"])
        kept = seen.get(key)
        if kept is None:
            seen[key] = item["evidence_id"]
            result.append(item)
        else:
            coverage.setdefault(kept, []).append(item["evidence_id"])
    return result, coverage
