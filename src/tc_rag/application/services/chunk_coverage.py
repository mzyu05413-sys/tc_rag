"""按来源区间检查实际文字覆盖；不把“Block 出现过”当作全文未丢失。"""

from typing import Any

from tc_rag.application.services.chunking import _table_rows
from tc_rag.domain.models import Chunk, DocumentBlock, Issue

Ranges = dict[str, list[tuple[int, int]]]


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(result[-1][1], end)
        else:
            result.append((start, end))
    return result


def missing_ranges(required: list[tuple[int, int]], covered: list[tuple[int, int]]) -> list[
    tuple[int, int]
]:
    """区间相减，避免按每个字符建集合；大文档也只按区间数占内存。"""
    covered = merge_ranges(covered)
    missing = []
    for start, end in merge_ranges(required):
        cursor = start
        for a, b in covered:
            if b <= cursor:
                continue
            if a >= end:
                break
            if a > cursor:
                missing.append((cursor, a))
            cursor = max(cursor, b)
            if cursor >= end:
                break
        if cursor < end:
            missing.append((cursor, end))
    return missing


def indexed_ranges(
    blocks: list[DocumentBlock], chunks: list[Chunk],
) -> tuple[dict[str, Ranges], list[dict[str, str]]]:
    """只索引来源和正文均有效的 Chunk；损坏输入不能得到漂亮的覆盖数字。"""
    by = {b.block_id: b for b in blocks}
    if len(by) != len(blocks):
        raise ValueError("原文 Block ID 重复")
    result: dict[str, Ranges] = {}
    invalid = []
    seen: set[str] = set()
    for chunk in chunks:
        reason = ""
        if chunk.chunk_id in seen:
            result.pop(chunk.chunk_id, None)
            reason = "duplicate_chunk_id"
        seen.add(chunk.chunk_id)
        parts: list[str] = []
        ranges: Ranges = {}
        for source in chunk.sources:
            block = by.get(source.block_id)
            if block is None or block.kind in {"image", "other"} or not (
                0 <= source.start_char < source.end_char <= len(block.content)
            ):
                reason = reason or "invalid_source"
                continue
            parts.append(block.content[source.start_char:source.end_char])
            ranges.setdefault(source.block_id, []).append((source.start_char, source.end_char))
        if not reason and chunk.text != "\n".join(parts):
            reason = "text_mismatch"
        if reason:
            invalid.append({"chunk_id": chunk.chunk_id, "reason": reason})
        else:
            result[chunk.chunk_id] = ranges
    return result, invalid


def coverage_diagnostics(
    blocks: list[DocumentBlock], chunks: list[Chunk], issues: list[Issue], target: int,
) -> dict[str, Any]:
    """表格检查完整数据行；JSON 包装留在 Block，不强制进入每个 Chunk。"""
    indexed, invalid = indexed_ranges(blocks, chunks)
    covered: Ranges = {}
    for ranges in indexed.values():
        for block_id, spans in ranges.items():
            covered.setdefault(block_id, []).extend(spans)
    required_characters = missing_characters = eligible = 0
    missing = []
    for block in blocks:
        if block.kind in {"image", "other"} or not block.content.strip():
            continue
        eligible += 1
        required = (_table_rows(block.content) if block.kind == "table" else []) or [
            (0, len(block.content))
        ]
        absent = missing_ranges(required, covered.get(block.block_id, []))
        required_characters += sum(b - a for a, b in required)
        missing_characters += sum(b - a for a, b in absent)
        if absent:
            missing.append({"block_id": block.block_id, "ranges": absent})
    warned = {obj for issue in issues for obj in issue.object_ids}
    unreported = [c.chunk_id for c in chunks
                  if c.token_estimate > target and c.chunk_id not in warned]
    return {
        "passed": not invalid and not missing and not unreported,
        "eligible_blocks": eligible, "invalid_chunks": invalid,
        "required_characters": required_characters, "missing_characters": missing_characters,
        "source_character_coverage": (
            (required_characters - missing_characters) / required_characters
            if required_characters else None
        ),
        "missing_ranges": missing, "oversize_without_issue": unreported,
        "note": "段落/标题检查原始字符，表格检查完整行区间；原文覆盖不是语义完整率。",
    }
