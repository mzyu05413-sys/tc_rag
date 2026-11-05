"""验证重叠分母、真实来源交集与章节诊断，不使用文本相似度代替来源。"""

import pytest

from tc_rag.application.services.chunk_stats import chunk_statistics
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock


def block(name, *, path=None, kind="paragraph", document="doc"):
    return DocumentBlock(
        block_id=name, document_id=document, order=0, kind=kind, content="abcdefghij",
        heading_path=["模块", "小节"] if path is None else path, locator={},
    )


def chunk(name, blocks, *ranges):
    by_id = {b.block_id: b for b in blocks}
    text = "\n".join(by_id[b].content[start:end] for b, start, end in ranges)
    return Chunk(
        chunk_id=name, fingerprint=name, text=text, token_estimate=len(text),
        sources=[ChunkSource(block_id=b, start_char=start, end_char=end)
                 for b, start, end in ranges],
    )


def test_same_words_in_different_blocks_are_not_source_overlap():
    blocks = [block("a"), block("b")]
    chunks = [chunk("left", blocks, ("a", 0, 10)), chunk("right", blocks, ("b", 0, 10))]
    stats = chunk_statistics(blocks, chunks, [], 100, len)
    overlap = stats["overlap"]
    assert overlap["eligible_pairs"] == 1 and overlap["overlapping_pairs"] == 0
    assert overlap["ratio"] == 0
    assert overlap["pairs"][0]["reason"] == "no_shared_source_ranges"


def test_intersection_is_merged_before_counting_characters_and_tokens():
    blocks = [block("a")]
    chunks = [chunk("left", blocks, ("a", 0, 8), ("a", 0, 8)),
              chunk("right", blocks, ("a", 3, 10), ("a", 5, 9))]
    overlap = chunk_statistics(blocks, chunks, [], 100, len)["overlap"]
    assert overlap["ratio"] == 1
    assert overlap["pairs"][0]["shared_characters"] == 5
    assert overlap["pairs"][0]["shared_tokens"] == 5
    assert overlap["token_distribution"]["max"] == 5
    assert overlap["pairs"][0]["shared_sources"] == [{
        "block_id": "a", "kind": "paragraph", "start_char": 3, "end_char": 8,
    }]


@pytest.mark.parametrize("other,reason", [
    (block("b", path=["模块", "另一节"]), "chapter_change"),
    (block("b", path=[]), "unknown_heading"),
    (block("b", document="other"), "different_documents"),
])
def test_ineligible_pairs_are_excluded_and_empty_denominator_is_null(other, reason):
    blocks = [block("a"), other]
    chunks = [chunk("left", blocks, ("a", 0, 10)), chunk("right", blocks, ("b", 0, 10))]
    overlap = chunk_statistics(blocks, chunks, [], 100, None)["overlap"]
    assert overlap["eligible_pairs"] == 0 and overlap["ratio"] is None
    assert overlap["excluded_reasons"] == {reason: 1}
    assert overlap["pairs"][0]["shared_tokens"] is None


def test_parent_heading_does_not_exclude_valid_body_chapter_overlap():
    blocks = [block("h", path=["模块"], kind="heading"), block("a")]
    chunks = [chunk("left", blocks, ("h", 0, 10), ("a", 0, 8)),
              chunk("right", blocks, ("a", 5, 10))]
    stats = chunk_statistics(blocks, chunks, [], 100, None)
    assert stats["overlap"]["eligible_pairs"] == 1 and stats["overlap"]["ratio"] == 1
    assert stats["overlap"]["pairs"][0]["shared_tokens"] is None
    assert stats["chapter_boundaries"]["mixed_top_level_count"] == 0


def test_known_mixed_chapters_and_unknown_headings_are_reported_separately():
    blocks = [block("a", path=["A"]), block("b", path=["B"]), block("c", path=[])]
    chunks = [chunk("mixed", blocks, ("a", 0, 10), ("b", 0, 10)),
              chunk("unknown", blocks, ("a", 0, 10), ("c", 0, 10))]
    chapters = chunk_statistics(blocks, chunks, [], 100, len)["chapter_boundaries"]
    assert chapters["mixed_top_level_count"] == 1
    assert chapters["mixed_top_level_ratio"] == 0.5
    assert chapters["mixed_top_level_chunks"] == ["mixed"]
    assert chapters["unknown_heading_chunks"] == ["unknown"]


def test_missing_source_is_not_misreported_as_valid_zero_overlap():
    blocks = [block("a")]
    left = chunk("left", blocks, ("a", 0, 8))
    right = chunk("right", blocks, ("a", 5, 10))
    right.sources[0].block_id = "missing"
    overlap = chunk_statistics(blocks, [left, right], [], 100, len)["overlap"]
    assert overlap["ratio"] is None
    assert overlap["excluded_reasons"] == {"invalid_sources": 1}


def test_empty_sample_has_no_fabricated_rates():
    stats = chunk_statistics([], [], [], 100, len)
    assert stats["overlap"]["ratio"] is None
    assert stats["chapter_boundaries"]["mixed_top_level_ratio"] is None
