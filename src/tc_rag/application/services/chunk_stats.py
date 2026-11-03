"""对已保存分块作离线统计，不重新解析、分块或调用模型。"""

from collections import Counter
from math import ceil
from statistics import median
from typing import Any

from tc_rag.application.services.chunking import TokenCounter
from tc_rag.domain.models import Chunk, DocumentBlock, Issue


def distribution(values: list[int]) -> dict[str, int | float | None]:
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": median(values) if values else None,
        "p90": ordered[max(0, ceil(len(values) * 0.9) - 1)] if values else None,
        "max": max(values) if values else None,
    }


def chunk_statistics(
    blocks: list[DocumentBlock],
    chunks: list[Chunk],
    issues: list[Issue],
    target: int,
    counter: TokenCounter | None,
) -> dict[str, Any]:
    by_id = {block.block_id: block for block in blocks}
    heading_only = 0
    per_chapter: Counter[str] = Counter()
    oversize = []
    for chunk in chunks:
        sources = [by_id[source.block_id] for source in chunk.sources if source.block_id in by_id]
        heading_only += bool(sources) and all(block.kind == "heading" for block in sources)
        roots = sorted(
            {block.heading_path[0] if block.heading_path else "未归类" for block in sources}
        )
        per_chapter.update(roots)
        if chunk.token_estimate > target:
            reasons = []
            if any(block.kind == "table" for block in sources):
                reasons.append("表格/表格行")
            if any(
                block.kind == "paragraph" and counter and counter(block.content) > target
                for block in sources
            ):
                reasons.append("超长段落")
            if any(
                "续表" in item.description and chunk.chunk_id in item.object_ids for item in issues
            ):
                reasons.append("续表补表头")
            oversize.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "tokens": chunk.token_estimate,
                    "reasons": reasons or ["完整单元/条件例外组，需核对"],
                }
            )
    visual = [block for block in blocks if block.origin == "vision"]
    return {
        "chunks": len(chunks),
        "tokens": distribution([chunk.token_estimate for chunk in chunks]),
        "small_chunks": {
            str(limit): {
                "count": sum(chunk.token_estimate < limit for chunk in chunks),
                "ratio": sum(chunk.token_estimate < limit for chunk in chunks) / len(chunks)
                if chunks
                else None,
            }
            for limit in (100, 200)
        },
        "heading_only_chunks": heading_only,
        "oversize": oversize,
        "per_top_chapter": dict(per_chapter),
        "chapter_count_note": (
            "按实际来源的一级章节计数；跨章节块可计入多个章节，不强制归属第一章。"
        ),
        "heading_blocks": sum(block.kind == "heading" for block in blocks),
        "empty_heading_ratio": sum(not block.heading_path for block in blocks) / len(blocks)
        if blocks
        else None,
        "visual_characters": distribution([len(block.content) for block in visual]),
        "visual_tokens": distribution([counter(block.content) for block in visual])
        if counter
        else None,
        "visual_blocks": [
            {
                "block_id": block.block_id,
                "characters": len(block.content),
                "tokens": counter(block.content) if counter else None,
            }
            for block in visual
        ],
    }
