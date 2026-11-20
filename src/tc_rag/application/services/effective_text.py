"""有效文本视图（A9）：去掉作废区间后得到面向模型的有效文本与区间映射。

原文（DocumentBlock.content）始终保留；这里只产生派生的有效视图，供组装模型
请求与检索索引时使用。区间映射 effective_ranges 返回未作废的原文左闭右开区间，
用于把有效文本中的证据回查回原文，避免删除线文字进入模型输入或可引用范围。
"""

from collections.abc import Sequence

from tc_rag.domain.models import DeprecatedRange, DocumentBlock


def effective_ranges(
    content: str, deprecated_ranges: Sequence[DeprecatedRange]
) -> list[tuple[int, int]]:
    """返回未被作废的原文 [start, end) 区间列表，按原文顺序合并重叠作废区间。"""
    ranges = sorted(deprecated_ranges, key=lambda item: (item.start_char, item.end_char))
    intervals: list[tuple[int, int]] = []
    cursor = 0
    for item in ranges:
        if item.start_char > cursor:
            intervals.append((cursor, item.start_char))
        cursor = max(cursor, item.end_char)
    if cursor < len(content):
        intervals.append((cursor, len(content)))
    return intervals


def effective_text(content: str, deprecated_ranges: Sequence[DeprecatedRange]) -> str:
    """按有效区间拼接有效文本；无作废区间时返回原内容。"""
    if not deprecated_ranges:
        return content
    ranges = effective_ranges(content, deprecated_ranges)
    return "".join(content[start:end] for start, end in ranges)


def effective_slice(
    content: str,
    start: int,
    end: int,
    deprecated_ranges: Sequence[DeprecatedRange],
) -> str:
    """返回 [start, end) 区间内未被作废的文本拼接；可能为空。"""
    if not deprecated_ranges:
        return content[start:end]
    parts: list[str] = []
    for eff_start, eff_end in effective_ranges(content, deprecated_ranges):
        overlap_start = max(start, eff_start)
        overlap_end = min(end, eff_end)
        if overlap_start < overlap_end:
            parts.append(content[overlap_start:overlap_end])
    return "".join(parts)


def effective_block(block: DocumentBlock) -> DocumentBlock:
    """返回去掉作废文字的块副本；无作废区间时返回原块，不写回原 Block。"""
    if not block.deprecated_ranges:
        return block
    return block.model_copy(
        update={
            "content": effective_text(block.content, block.deprecated_ranges),
            "deprecated_ranges": [],
        }
    )
