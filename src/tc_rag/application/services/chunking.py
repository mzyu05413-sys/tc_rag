"""保留原始字符映射的结构分块；不会删除否定词、单位或改写原文。"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    refined, header_issues = _repeat_cross_page_headers(refined, blocks, target, counter)
    issues.extend(header_issues)
    return ChunkingResult(refined, issues)


def _table_row_values(text: str, span: tuple[int, int]) -> list[Any] | None:
    try:
        value = json.loads(text[span[0] : span[1]])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, list) else None


def _normalized_row(row: list[Any] | None) -> tuple[str, ...]:
    if row is None:
        return ()
    return tuple(" ".join(str(cell).split()) if cell is not None else "" for cell in row)


def _header_index(rows: list[list[Any] | None]) -> int | None:
    """取非空单元格最多的一行作为表头；同分取最早，避免把续页数据行当表头。"""
    if not rows:
        return None
    scores = [
        sum(cell not in (None, "") for cell in row) if row is not None else 0 for row in rows
    ]
    return max(range(len(rows)), key=lambda index: (scores[index], -index))


@dataclass(frozen=True)
class TableContinuation:
    root_id: str
    continuation_id: str
    header_start: int
    header_end: int


def cross_page_table_continuations(blocks: list[DocumentBlock]) -> list[TableContinuation]:
    """识别 PDF 跨页续表，返回续页 Block 与根表头区间；不修改任何 Block。"""
    tables = sorted(
        (block for block in blocks if block.kind == "table"),
        key=lambda block: block.order,
    )
    spans = {block.block_id: _table_rows(block.content) for block in tables}
    rows = {
        block.block_id: [
            _table_row_values(block.content, span) for span in spans[block.block_id]
        ]
        for block in tables
    }
    continuation: dict[str, str] = {}
    for previous, current in zip(tables, tables[1:]):
        previous_page = previous.locator.page
        current_page = current.locator.page
        if previous_page is None or current_page != previous_page + 1:
            continue
        if previous.heading_path != current.heading_path:
            continue
        if any(
            block.locator.page == previous_page and block.order > previous.order
            for block in tables
        ):
            continue
        if any(
            block.locator.page == current_page and block.order < current.order
            for block in tables
        ):
            continue
        previous_rows = rows[previous.block_id]
        current_rows = rows[current.block_id]
        previous_header = _header_index(previous_rows)
        if previous_header is None or not current_rows:
            continue
        header = _normalized_row(previous_rows[previous_header])
        first = _normalized_row(current_rows[0])
        if not header or not first or len(header) != len(first):
            continue
        if first == header:
            continue
        continuation[current.block_id] = previous.block_id

    result: list[TableContinuation] = []
    for block_id in continuation:
        root_id = block_id
        while root_id in continuation:
            root_id = continuation[root_id]
        root_spans = spans.get(root_id) or []
        root_header = _header_index(rows.get(root_id) or [])
        if root_header is None or not root_spans:
            continue
        header_start, header_end = root_spans[root_header]
        result.append(
            TableContinuation(
                root_id=root_id,
                continuation_id=block_id,
                header_start=header_start,
                header_end=header_end,
            )
        )
    return result


def _repeat_cross_page_headers(
    chunks: list[Chunk],
    blocks: list[DocumentBlock],
    target: int,
    counter: TokenCounter,
) -> tuple[list[Chunk], list[Issue]]:
    """PDF 跨页续表补入起始表头；只重复来源区间，不改写任何原文。

    解析库按页产出独立 table Block，续页片段没有表头。这里只在前一页最后一张表
    与后一页第一张表同章节、同列数且首页首行不是重复表头时补入起始表头；
    多页续表沿映射回溯到第一页表头。
    """
    by_id = {block.block_id: block for block in blocks}
    usage: dict[str, list[int]] = {}
    for index, chunk in enumerate(chunks):
        for source in chunk.sources:
            usage.setdefault(source.block_id, []).append(index)

    issues: list[Issue] = []
    for item in cross_page_table_continuations(blocks):
        root = by_id.get(item.root_id)
        if root is None:
            continue
        header_text = root.content[item.header_start : item.header_end]
        for index in sorted(set(usage.get(item.continuation_id, []))):
            chunk = chunks[index]
            if any(
                source.block_id == item.root_id
                and source.start_char == item.header_start
                and source.end_char == item.header_end
                for source in chunk.sources
            ):
                continue
            sources = [
                ChunkSource(
                    block_id=item.root_id,
                    start_char=item.header_start,
                    end_char=item.header_end,
                ),
                *chunk.sources,
            ]
            text = header_text + "\n" + chunk.text
            digest = fingerprint([text, [source.model_dump() for source in sources]])
            updated = Chunk(
                chunk_id="chunk_" + digest,
                text=text,
                token_estimate=counter(text),
                fingerprint=digest,
                sources=sources,
            )
            chunks[index] = updated
            if updated.token_estimate > target:
                issues.append(
                    Issue(
                        issue_id=new_id("issue"),
                        kind="gap",
                        severity="warning",
                        object_ids=[updated.chunk_id],
                        description="跨页续表补入表头后超出分块预算，未截断原文",
                    )
                )
    return chunks, issues


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
