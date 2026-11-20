"""有效文本视图（A9）：去掉作废区间，保留原文与区间映射。"""

from tc_rag.application.services.effective_text import (
    effective_block,
    effective_ranges,
    effective_slice,
    effective_text,
)
from tc_rag.domain.models import DeprecatedRange, DocumentBlock, Locator


def dr(start, end):
    return DeprecatedRange(start_char=start, end_char=end)


def test_no_deprecated_returns_original():
    assert effective_text("abcdef", []) == "abcdef"


def test_middle_range_is_stripped():
    assert effective_text("abcdef", [dr(2, 4)]) == "abef"


def test_leading_and_trailing_ranges_are_stripped():
    assert effective_text("abcdef", [dr(0, 2)]) == "cdef"
    assert effective_text("abcdef", [dr(4, 6)]) == "abcd"


def test_effective_ranges_split_around_deprecated():
    assert effective_ranges("abcdef", [dr(2, 4)]) == [(0, 2), (4, 6)]


def test_overlapping_ranges_are_merged():
    assert effective_ranges("abcdef", [dr(1, 3), dr(2, 4)]) == [(0, 1), (4, 6)]
    assert effective_text("abcdef", [dr(1, 3), dr(2, 4)]) == "aef"


def test_effective_slice_strips_deprecated_within_interval():
    assert effective_slice("abcdef", 0, 6, [dr(2, 4)]) == "abef"
    assert effective_slice("abcdef", 1, 5, [dr(2, 4)]) == "be"


def test_effective_slice_empty_when_fully_deprecated():
    assert effective_slice("abcdef", 2, 4, [dr(2, 4)]) == ""


def test_effective_block_strips_deprecated_and_clears_ranges():
    block = DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content="abcdef", locator=Locator(page=1), deprecated_ranges=[dr(2, 4)],
    )
    result = effective_block(block)
    assert result.content == "abef"
    assert result.deprecated_ranges == []
    # 原始块不变，不写回。
    assert block.content == "abcdef"


def test_effective_block_noop_without_deprecated():
    block = DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content="abcdef", locator=Locator(page=1),
    )
    assert effective_block(block) is block
