"""保留原始字符映射的结构分块；不会删除否定词、单位或改写原文。"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock, EvidenceSpan, Issue

TokenCounter = Callable[[str], int]


def character_estimate(text: str) -> int:
    """离线粗估，一字符按一个 token；只用于规划，不能作为计费数据。"""
    return len(text)


def normalized_quote(text: str) -> str:
    """只在引文核验时兼容全半角及排版空白，原始 Block 不变。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def verify_evidence(evidence: EvidenceSpan, block: DocumentBlock) -> bool:
    quote = normalized_quote(evidence.quote)
    return bool(
        evidence.origin == block.origin
        and evidence.processing_model == block.processing_model
        and evidence.processing_version == block.processing_version
        and evidence.block_id == block.block_id
        and evidence.locator == block.locator
        and quote
        and quote in normalized_quote(block.content)
    )


@dataclass
class ChunkingResult:
    chunks: list[Chunk]
    issues: list[Issue]


def structural_chunks(
    blocks: list[DocumentBlock],
    target: int,
    overlap: int,
    counter: TokenCounter = character_estimate,
) -> ChunkingResult:
    """按章节与完整段落切分；条件后的例外作为原子组，超预算显式报问题。

    大表按行拆分并重复首行；超长段落保留整块，由调用方拒绝超上下文请求。
    页眉页脚由解析器隔离；不凭重复出现便删除可能有效的业务文字。
    """
    if target <= 0 or not 0 <= overlap < target:
        raise ValueError("分块预算必须为正，且重叠小于目标预算")
    units: list[list[DocumentBlock]] = []
    for block in sorted(blocks, key=lambda item: item.order):
        if block.kind == "image" or not block.content.strip():
            continue
        if units and re.match(r"^(但是|但|除非|例外|except\b|unless\b)", block.content.strip()):
            units[-1].append(block)
        else:
            units.append([block])
    chunks: list[Chunk] = []
    issues: list[Issue] = []
    pending: list[DocumentBlock] = []

    def text_of(items: list[DocumentBlock]) -> str:
        return "\n".join(item.content for item in items)

    def emit() -> None:
        if not pending:
            return
        text = text_of(pending)
        sources = [
            ChunkSource(block_id=b.block_id, start_char=0, end_char=len(b.content)) for b in pending
        ]
        digest = fingerprint([text, [s.model_dump() for s in sources]])
        chunk = Chunk(
            chunk_id="chunk_" + digest,
            text=text,
            token_estimate=counter(text),
            fingerprint=digest,
            sources=sources,
        )
        chunks.append(chunk)
        if chunk.token_estimate > target:
            issues.append(
                Issue(
                    issue_id=new_id("issue"),
                    kind="gap",
                    severity="warning",
                    object_ids=[chunk.chunk_id],
                    description="完整段落/表格/条件例外组超出分块预算，未截断原文",
                )
            )

    for unit in units:
        chapter_changed = bool(pending and pending[-1].heading_path != unit[0].heading_path)
        if pending and (chapter_changed or counter(text_of(pending + unit)) > target):
            emit()
            # 重叠仅保留完整尾段，且不能让新块因重叠再次超预算。
            tail: list[DocumentBlock] = []
            if not chapter_changed:
                for item in reversed(pending):
                    candidate = [item] + tail
                    if counter(text_of(candidate)) > overlap:
                        break
                    tail = candidate
            pending = tail if counter(text_of(tail + unit)) <= target else []
        pending.extend(unit)
    emit()
    # 大表按完整行分组；保留首行作为上下文，不擅自宣称它一定是表头。
    refined: list[Chunk] = []
    by_id = {b.block_id: b for b in blocks}
    for chunk in chunks:
        if len(chunk.sources) != 1 or chunk.token_estimate <= target:
            refined.append(chunk)
            continue
        block = by_id[chunk.sources[0].block_id]
        spans = _table_rows(block.content) if block.kind == "table" else []
        if len(spans) < 2:
            refined.append(chunk)
            continue
        header = spans[0]
        group = [header]

        def table_chunk(rows: list[tuple[int, int]]) -> Chunk:
            text = "\n".join(block.content[start:end] for start, end in rows)
            sources = [
                ChunkSource(block_id=block.block_id, start_char=start, end_char=end)
                for start, end in rows
            ]
            digest = fingerprint([text, [s.model_dump() for s in sources]])
            return Chunk(
                chunk_id="chunk_" + digest,
                text=text,
                token_estimate=counter(text),
                fingerprint=digest,
                sources=sources,
            )

        for span in spans[1:]:
            if len(group) > 1 and table_chunk(group + [span]).token_estimate > target:
                refined.append(table_chunk(group))
                group = [header]
            group.append(span)
        if len(group) > 1:
            refined.append(table_chunk(group))
    return ChunkingResult(refined, issues)


def _table_rows(text: str) -> list[tuple[int, int]]:
    """JSONDecoder 提取原始 JSON 行的准确区间，不用重新序列化后的文字冒充原文。"""
    match = re.search(r'"rows"\s*:\s*\[', text)
    if match is None:
        return []
    decoder = json.JSONDecoder()
    position = match.end()
    spans: list[tuple[int, int]] = []
    try:
        while position < len(text):
            while position < len(text) and text[position] in " \r\n\t,":
                position += 1
            if text[position] == "]":
                return spans
            value, end = decoder.raw_decode(text, position)
            if not isinstance(value, list):
                return []
            spans.append((position, end))
            position = end
    except (ValueError, IndexError):
        return []
    return []
