"""按真实来源区间诊断章节边界与相邻重叠；不把词面相同视为共享原文。"""

from collections import Counter
from typing import Any

from tc_rag.application.services.chunking import TokenCounter
from tc_rag.domain.models import Chunk, DocumentBlock


def _chapter(chunk: Chunk, blocks: dict[str, DocumentBlock]) -> tuple[str, tuple[str, ...]] | str:
    """标题前缀使用正文所属章节；多小节合并块不能假定只属于最后一个小节。"""
    if not chunk.sources or any(
        s.block_id not in blocks or not 0 <= s.start_char < s.end_char <= len(
            blocks[s.block_id].content
        ) for s in chunk.sources
    ):
        return "invalid_sources"
    sources = [blocks[s.block_id] for s in chunk.sources]
    documents = {b.document_id for b in sources}
    if len(documents) != 1:
        return "mixed_documents"
    body = [b for b in sources if b.kind != "heading"] or sources
    if any(not b.heading_path for b in body):
        return "unknown_heading"
    paths = {tuple(b.heading_path) for b in body}
    if len(paths) != 1:
        return "mixed_sections"
    path = next(iter(paths))
    if any(b.heading_path and path[:len(b.heading_path)] != tuple(b.heading_path)
           for b in sources if b.kind == "heading"):
        return "unrelated_headings"
    return next(iter(documents)), path


def _shared_ranges(
    left: Chunk, right: Chunk, blocks: dict[str, DocumentBlock],
) -> list[dict[str, Any]]:
    """合并交集区间，避免重复表头或同源区间重复出现导致字符/token 重复计数。"""
    intersections: dict[str, list[tuple[int, int]]] = {}
    for a in left.sources:
        for b in right.sources:
            if a.block_id != b.block_id:
                continue
            start, end = max(a.start_char, b.start_char), min(a.end_char, b.end_char)
            if start < end:
                intersections.setdefault(a.block_id, []).append((start, end))
    shared: list[dict[str, Any]] = []
    for block_id, ranges in intersections.items():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = merged[-1][0], max(merged[-1][1], end)
            else:
                merged.append((start, end))
        shared.extend({
            "block_id": block_id, "start_char": start, "end_char": end,
            "kind": blocks[block_id].kind,
        } for start, end in merged)
    return shared


def boundary_statistics(
    blocks: list[DocumentBlock], chunks: list[Chunk], counter: TokenCounter | None,
) -> dict[str, Any]:
    """重叠率只使用可确认属于同文档、同完整章节的相邻块；分母为空时为 null。"""
    by_id = {b.block_id: b for b in blocks}
    mixed_roots, mixed_documents, unknown = [], [], []
    for chunk in chunks:
        sources = [by_id[s.block_id] for s in chunk.sources if s.block_id in by_id]
        if len({b.document_id for b in sources}) > 1:
            mixed_documents.append(chunk.chunk_id)
        # 未归类不能当作另一个已知章节；它的存在另行报告。
        if len({(b.document_id, b.heading_path[0]) for b in sources if b.heading_path}) > 1:
            mixed_roots.append(chunk.chunk_id)
        if not sources or any(not b.heading_path for b in sources):
            unknown.append(chunk.chunk_id)
    pairs = []
    excluded: Counter[str] = Counter()
    eligible = hits = 0
    for left, right in zip(chunks, chunks[1:]):
        a, b = _chapter(left, by_id), _chapter(right, by_id)
        reason = a if isinstance(a, str) else b if isinstance(b, str) else None
        if reason is None and a != b:
            assert isinstance(a, tuple) and isinstance(b, tuple)
            reason = "different_documents" if a[0] != b[0] else "chapter_change"
        item: dict[str, Any] = {
            "left_chunk_id": left.chunk_id, "right_chunk_id": right.chunk_id,
            "eligible": reason is None, "reason": reason,
            "shared_sources": [], "shared_characters": None, "shared_tokens": None,
        }
        if reason is not None:
            excluded[reason] += 1
        else:
            eligible += 1
            shared = _shared_ranges(left, right, by_id)
            hits += bool(shared)
            shared_text = "\n".join(
                by_id[s["block_id"]].content[s["start_char"]:s["end_char"]] for s in shared
            )
            item.update(
                reason="shared_source_ranges" if shared else "no_shared_source_ranges",
                shared_sources=shared,
                shared_characters=sum(s["end_char"] - s["start_char"] for s in shared),
                shared_tokens=counter(shared_text) if counter is not None else None,
            )
        pairs.append(item)
    return {
        "chapter_boundaries": {
            "mixed_top_level_count": len(mixed_roots),
            "mixed_top_level_ratio": len(mixed_roots) / len(chunks) if chunks else None,
            "mixed_top_level_chunks": mixed_roots,
            "mixed_document_count": len(mixed_documents),
            "mixed_document_chunks": mixed_documents,
            "unknown_heading_count": len(unknown), "unknown_heading_chunks": unknown,
        },
        "overlap": {
            "adjacent_pairs": len(pairs), "eligible_pairs": eligible,
            "overlapping_pairs": hits, "ratio": hits / eligible if eligible else None,
            "excluded_reasons": dict(excluded), "pairs": pairs,
            "note": "按同文档、同完整章节的来源区间交集计算；包含重复表头。"
                    "无共享区间不等于丢失需求，未据快照推断具体调度原因。",
        },
    }
