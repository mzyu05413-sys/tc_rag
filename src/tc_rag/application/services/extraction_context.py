"""提炼邻文只作背景：有限窗口、原始区间、显式来源，不能扩展证据授权范围。"""

import json
from dataclasses import dataclass
from typing import Any

from tc_rag.application.services.chunking import TokenCounter, _table_rows
from tc_rag.domain.models import Chunk, DocumentBlock

CONTEXT_POLICY = "source_window_v2"


@dataclass(frozen=True)
class _Window:
    block: DocumentBlock
    start: int
    end: int
    position: str
    distance: int = 1


def _outside_ranges(size: int, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """求授权区间的补集；重复表头、相邻和重叠 sources 不能把核心文字漏入背景。"""
    cursor = 0
    output = []
    for start, end in sorted(ranges):
        if start > cursor:
            output.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < size:
        output.append((cursor, size))
    return output


def _windows(chunk: Chunk, blocks: list[DocumentBlock], limit: int) -> list[_Window]:
    by = {b.block_id: b for b in blocks}
    if len(by) != len(blocks):
        raise ValueError("邻文原文 Block ID 重复")
    ranges: dict[str, list[tuple[int, int]]] = {}
    for source in chunk.sources:
        block = by.get(source.block_id)
        if block is None or not (0 <= source.start_char < source.end_char <= len(block.content)):
            raise ValueError("邻文对应的 Chunk 来源区间无效")
        ranges.setdefault(source.block_id, []).append((source.start_char, source.end_char))
    positions = [i for i, b in enumerate(blocks) if b.block_id in ranges]
    if not positions or len({blocks[i].document_id for i in positions}) != 1:
        return []
    first, last = min(positions), max(positions)
    document = blocks[first].document_id
    groups: dict[str, list[_Window]] = {key: [] for key in ("before", "after", "between")}
    for i in positions:
        block = blocks[i]
        # 表格只提供真实行片段，不把 JSON 包装字符当成缺失业务背景。
        rows = _table_rows(block.content) if block.kind == "table" else []
        first_char = min(a for a, _ in ranges[block.block_id])
        last_char = max(z for _, z in ranges[block.block_id])
        for start, end in _outside_ranges(len(block.content), ranges[block.block_id]):
            position = ("before" if i == first and end <= first_char
                        else "after" if i == last and start >= last_char else "between")
            pieces = ([(max(start, a), min(end, z)) for a, z in rows
                       if max(start, a) < min(end, z)] if rows else [(start, end)])
            groups[position].extend(_Window(block, a, z, position) for a, z in pieces
                                    if block.content[a:z].strip())
    # 前文及内部空隙取靠近后方核心区间的一端；表头与本批数据行之间可能有多行。
    groups["before"].reverse()
    groups["between"].reverse()
    for direction, indexes in (("before", range(first - 1, -1, -1)),
                               ("after", range(last + 1, len(blocks)))):
        for i in indexes:
            block = blocks[i]
            if block.document_id != document:
                break
            if len(groups[direction]) >= limit:
                break
            if block.kind in {"image", "other"} or not block.content.strip():
                continue
            groups[direction].append(_Window(
                block, 0, len(block.content), direction, len(groups[direction]) + 1,
            ))
    # 每侧至多 N 个片段；内部空隙单独至多 N 个。总字符预算由调用方统一分配。
    return [window for group in groups.values() for window in group[:limit]]


def neighboring_context(
    chunk: Chunk, blocks: list[DocumentBlock], limit: int, char_budget: int,
) -> list[dict[str, Any]]:
    if limit < 0 or char_budget < 0:
        raise ValueError("邻文窗口和字符预算不能为负")
    if not limit or not char_budget:
        return []
    blocks = sorted(blocks, key=lambda block: block.order)
    candidates = sorted(_windows(chunk, blocks, limit), key=lambda item: item.distance)
    # 极小预算优先保留近邻，不能因片段数多于字符数导致全部背景消失。
    candidates = candidates[:char_budget]
    share = char_budget // len(candidates) if candidates else 0
    source_ids = {s.block_id for s in chunk.sources}
    output = []
    for window in candidates:
        if not share:
            break
        block, direction = window.block, window.position
        start = (max(window.start, window.end - share)
                 if direction in {"before", "between"} else window.start)
        end = min(window.end, start + share)
        output.append({
            "block_id": block.block_id, "position": direction,
            "distance": window.distance,
            "heading_path": block.heading_path, "locator": block.locator.model_dump(mode="json"),
            "origin": block.origin, "processing_model": block.processing_model,
            "processing_version": block.processing_version,
            "start_char": start, "end_char": end, "text": block.content[start:end],
            "truncated": start != 0 or end != len(block.content), "role": "background_only",
            "scope": "same_block" if block.block_id in source_ids
            else "adjacent_block",
        })
    return output


def fit_context_budget(
    request: dict[str, Any], schema: dict[str, Any], maximum: int, counter: TokenCounter,
) -> None:
    """预算不足时先删较远背景，不裁当前 Chunk、原文、Schema 或事实约束。"""
    context = request.get("neighbor_context", [])
    omitted: list[str] = list(request.get("neighbor_context_omitted", []))
    while context and counter(json.dumps(
        {**request, "output_schema": schema}, ensure_ascii=False
    )) > maximum:
        # 先删除较远项；同距离时优先保留前文，不固定牺牲整侧背景。
        index = max(range(len(context)), key=lambda i: (context[i]["distance"], i))
        victim = context.pop(index)
        omitted.append(victim["block_id"])
        request["neighbor_context_omitted"] = list(omitted)
