"""向量片段细分保留所有字符与原文区间，支持多字节字符。"""

from tc_rag.application.kb.input_limits import bound_chunks
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock


def test_bounded_chunks_preserve_unicode_and_source_ranges():
    text = "审批条件必须全部满足。🙂" * 100
    block = DocumentBlock(block_id="b", document_id="d", kind="paragraph", order=0,
                          content=text, locator={})
    chunk = Chunk(chunk_id="c", text=text, token_estimate=len(text), fingerprint="f",
                  sources=[ChunkSource(block_id="b", start_char=0, end_char=len(text))])
    pieces = bound_chunks([chunk], [block], 90, len)
    assert "".join(piece.text for piece in pieces) == text
    assert all(len(piece.text.encode("utf-8")) <= 90 for piece in pieces)
    assert all(text[p.sources[0].start_char:p.sources[0].end_char] == p.text for p in pieces)
    assert pieces[0].sources[0].start_char == 0
    assert pieces[-1].sources[0].end_char == len(text)
