"""仅细分超预算的检索片段，完整保留原 Block 和字符来源。"""

from tc_rag.application.services.chunking import TokenCounter
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock


def bound_chunks(
    chunks: list[Chunk], blocks: list[DocumentBlock], limit: int | None, counter: TokenCounter,
) -> list[Chunk]:
    if limit is None:
        return chunks
    if limit < 4:
        raise ValueError("UTF-8 片段字节预算过小")
    block_map = {block.block_id: block for block in blocks}
    result = []
    for chunk in chunks:
        if len(chunk.text.encode("utf-8")) <= limit:
            result.append(chunk)
            continue
        for source in chunk.sources:
            text = block_map[source.block_id].content
            start = source.start_char
            while start < source.end_char:
                remaining = text[start:source.end_char]
                part = remaining.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
                boundary = max(part.rfind(mark) for mark in "。！？；\n ")
                if boundary >= len(part) // 2:
                    part = part[:boundary + 1]
                end = start + len(part)
                span = ChunkSource(block_id=source.block_id, start_char=start, end_char=end)
                identity = fingerprint([part, span.model_dump(), "embedding_bytes_v1", limit])
                result.append(Chunk(
                    chunk_id=identity, text=part, token_estimate=counter(part),
                    fingerprint=identity, sources=[span],
                ))
                start = end
    return result
