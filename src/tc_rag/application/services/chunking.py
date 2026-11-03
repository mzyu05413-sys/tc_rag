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


@dataclass(frozen=True)
class _Piece:
    block: DocumentBlock
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.block.content[self.start : self.end]


def _coalesce(items: list[_Piece]) -> list[_Piece]:
    """同一原段内相邻区间重新连回连续文字，不插入不存在的换行。"""
    output: list[_Piece] = []
    for item in items:
        if (
            output
            and output[-1].block.block_id == item.block.block_id
            and output[-1].end == item.start
        ):
            output[-1] = _Piece(item.block, output[-1].start, item.end)
        else:
            output.append(item)
    return output


def _piece_chunk(items: list[_Piece], counter: TokenCounter) -> Chunk:
    items = _coalesce(items)
    text = "\n".join(item.text for item in items)
    sources = [
        ChunkSource(block_id=p.block.block_id, start_char=p.start, end_char=p.end) for p in items
    ]
    digest = fingerprint([text, [source.model_dump() for source in sources]])
    return Chunk(
        chunk_id="chunk_" + digest,
        text=text,
        token_estimate=counter(text),
        fingerprint=digest,
        sources=sources,
    )


_EXCEPTION = re.compile(r"^(但是|但|除非|例外|except\b|unless\b)")


def _paragraph_pieces(block: DocumentBlock, target: int, counter: TokenCounter) -> list[_Piece]:
    """只使用原文句界；无法切开的长句保留，不按字符数硬截断。"""
    boundaries = [
        match.end()
        for match in re.finditer(
            r"\n+|[。！？；!?;]+[\"'’”）)]*|(?<!\d)\.(?!\d)[\"'’”）)]*", block.content
        )
    ]
    boundaries.append(len(block.content))
    atoms: list[_Piece] = []
    start = 0
    for end in sorted(set(boundaries)):
        if end <= start:
            continue
        part = _Piece(block, start, end)
        if atoms and (not part.text.strip() or _EXCEPTION.match(part.text.strip())):
            atoms[-1] = _Piece(block, atoms[-1].start, end)
        else:
            atoms.append(part)
        start = end
    pieces: list[_Piece] = []
    for atom in atoms:
        if pieces and counter(block.content[pieces[-1].start : atom.end]) <= target:
            pieces[-1] = _Piece(block, pieces[-1].start, atom.end)
        else:
            pieces.append(atom)
    return pieces


def structural_chunks(
    blocks: list[DocumentBlock],
    target: int,
    overlap: int,
    counter: TokenCounter = character_estimate,
    *,
    attach_headings: bool = False,
    min_tokens: int = 0,
    split_long_paragraphs: bool = False,
) -> ChunkingResult:
    """按章节和原文区间打包；不删除原文，不把段落中间硬切断。

    默认保留旧打包行为；标题、小章节与长段策略均显式开启。
    疑似页眉页脚仅在解析器显式隔离后跳过，不凭重复文字自动删除。
    """
    if target <= 0 or not 0 <= overlap < target or not 0 <= min_tokens < target:
        raise ValueError("分块预算必须为正，重叠和最小分块均小于目标预算")
    units: list[list[_Piece]] = []
    issues: list[Issue] = []
    enhanced = attach_headings or bool(min_tokens) or split_long_paragraphs
    for block in sorted(blocks, key=lambda item: item.order):
        if block.kind in {"image", "other"} or not block.content.strip():
            continue
        pieces = [_Piece(block, 0, len(block.content))]
        if split_long_paragraphs and block.kind == "paragraph" and counter(block.content) > target:
            pieces = _paragraph_pieces(block, target, counter)
            if len(pieces) > 1:
                issues.append(
                    Issue(
                        issue_id=new_id("issue"),
                        kind="gap",
                        severity="warning",
                        object_ids=[block.block_id],
                        description=f"超长段落已按句拆分，上下文可能被割裂；拆分段数 {len(pieces)}",
                    )
                )
        for piece in pieces:
            same_chapter = bool(units and units[-1][-1].block.heading_path == block.heading_path)
            if units and _EXCEPTION.match(piece.text.strip()) and (same_chapter or not enhanced):
                units[-1].append(piece)
            else:
                units.append([piece])
    chunks: list[Chunk] = []
    pending: list[_Piece] = []
    pending_units: list[list[_Piece]] = []

    def text_of(items: list[_Piece]) -> str:
        return "\n".join(piece.text for piece in _coalesce(items))

    def emit() -> None:
        if pending:
            chunks.append(_piece_chunk(pending, counter))

    for original_unit in units:
        unit = original_unit
        changed = bool(pending and pending[-1].block.heading_path != unit[0].block.heading_path)
        # 标题链随下文进入同一单元，不制造孤立标题请求；末尾标题仍由 emit 保存。
        if attach_headings and pending and all(p.block.kind == "heading" for p in pending):
            if changed or unit[0].block.kind != "heading":
                unit = pending + unit
                pending, pending_units = [], []
                changed = False
        if changed and min_tokens and counter(text_of(pending)) < min_tokens:
            left, right = pending[-1].block.heading_path, unit[0].block.heading_path
            related = bool(
                left
                and right
                and left[0] == right[0]
                and (
                    (len(left) > 1 and left[:-1] == right[:-1])
                    or (len(right) > len(left) and right[: len(left)] == left)
                )
            )
            if related and counter(text_of(pending + unit)) <= target:
                # 合并跨小节时不使用重叠；每个来源仍保留自己的章节。
                pending.extend(unit)
                pending_units.append(unit)
                continue
        if pending and (changed or counter(text_of(pending + unit)) > target):
            emit()
            tail: list[_Piece] = []
            tail_units: list[list[_Piece]] = []
            if not changed:
                candidates = pending_units if enhanced else [[part] for part in pending]
                for previous in reversed(candidates):
                    if counter(text_of(previous + tail)) > overlap:
                        break
                    tail = previous + tail
                    tail_units.insert(0, previous)
            if counter(text_of(tail + unit)) > target:
                tail, tail_units = [], []
            pending, pending_units = tail, tail_units
        pending.extend(unit)
        pending_units.append(unit)
    emit()
    refined: list[Chunk] = []
    by_id = {block.block_id: block for block in blocks}
    for chunk in chunks:
        # 单表或“标题前缀+单表”按行分组；前缀仅保留到第一组。
        table_sources = [
            source for source in chunk.sources if by_id[source.block_id].kind == "table"
        ]
        prefix_sources = [
            source for source in chunk.sources if by_id[source.block_id].kind != "table"
        ]
        can_split = len(table_sources) == 1 and all(
            by_id[source.block_id].kind == "heading" for source in prefix_sources
        )
        if not can_split or chunk.token_estimate <= target:
            refined.append(chunk)
            continue
        block = by_id[table_sources[0].block_id]
        spans = _table_rows(block.content)
        if len(spans) < 2:
            refined.append(chunk)
            continue
        prefix = [_Piece(by_id[s.block_id], s.start_char, s.end_char) for s in prefix_sources]
        header = spans[0]
        group = [header]

        def table_chunk(rows: list[tuple[int, int]]) -> Chunk:
            return _piece_chunk(
                prefix + [_Piece(block, start, end) for start, end in rows], counter
            )

        for span in spans[1:]:
            if len(group) > 1 and table_chunk(group + [span]).token_estimate > target:
                refined.append(table_chunk(group))
                prefix = []
                group = [header]
            group.append(span)
        if len(group) > 1:
            refined.append(table_chunk(group))
    refined, header_issues = _repeat_cross_page_headers(refined, blocks, target, counter)
    issues.extend(header_issues)
    for chunk in refined:
        if chunk.token_estimate > target and not any(
            chunk.chunk_id in issue.object_ids for issue in issues
        ):
            issues.append(
                Issue(
                    issue_id=new_id("issue"),
                    kind="gap",
                    severity="warning",
                    object_ids=[chunk.chunk_id],
                    description="完整段落/表格/条件例外组超出分块预算，未截断原文",
                )
            )
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
    scores = [sum(cell not in (None, "") for cell in row) if row is not None else 0 for row in rows]
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
        block.block_id: [_table_row_values(block.content, span) for span in spans[block.block_id]]
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
            block.locator.page == previous_page and block.order > previous.order for block in tables
        ):
            continue
        if any(
            block.locator.page == current_page and block.order < current.order for block in tables
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
