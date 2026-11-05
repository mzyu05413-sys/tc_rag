"""可选分块增强：完整尾句重叠、表格完整行预分组，不猜测首行就是表头。"""

import json

from tc_rag.application.services.chunking import (
    _EXCEPTION,
    TokenCounter,
    _coalesce,
    _Piece,
    _sentence_atoms,
    _table_rows,
    cross_page_table_continuations,
)
from tc_rag.application.services.design import issue
from tc_rag.domain.models import DocumentBlock, Issue


def table_context_issues(blocks: list[DocumentBlock]) -> list[Issue]:
    """旧的跨页检测仅作风险候选；缺乏明确承接关系时不把猜测表头注入正文。"""
    by_id = {b.block_id: b for b in blocks}
    return [issue(
        "gap", "PDF 相邻页表格疑似续表，但表头/承接关系未确认；未自动补入其他表格的首行",
        [item.root_id, item.continuation_id],
    ) for item in cross_page_table_continuations(blocks)
        if by_id[item.root_id].document_id == by_id[item.continuation_id].document_id]


def _text(pieces: list[_Piece]) -> str:
    return "\n".join(p.text for p in _coalesce(pieces))


def overlap_tail(
    units: list[list[_Piece]], following: list[_Piece], budget: int, target: int,
    counter: TokenCounter,
) -> list[list[_Piece]]:
    """先取完整单元，再尝试其尾句；条件例外和完整表格行均不截断。"""
    tail: list[list[_Piece]] = []
    first = following[0].block

    def fits(candidate: list[list[_Piece]]) -> bool:
        flat = [p for group in candidate for p in group]
        return counter(_text(flat)) <= budget and counter(_text(flat + following)) <= target

    for unit in reversed(units):
        if any(
            p.block.document_id != first.document_id or p.block.heading_path != first.heading_path
            for p in unit
        ):
            break
        if fits([unit, *tail]):
            tail.insert(0, unit)
            continue
        atoms: list[list[_Piece]] = []
        for piece in unit:
            parts = _sentence_atoms(piece) if piece.block.kind == "paragraph" else [piece]
            for part in parts:
                if atoms and _EXCEPTION.match(part.text.strip()):
                    atoms[-1].append(part)
                else:
                    atoms.append([part])
        for atom in reversed(atoms):
            if not fits([atom, *tail]):
                break
            tail.insert(0, atom)
        break
    return tail


def table_units(
    block: DocumentBlock, target: int, counter: TokenCounter,
) -> tuple[list[list[_Piece]], list[Issue]]:
    """只在超预算表格中分组；表头须有解析器给出的明确重复表头标记。"""
    whole = [_Piece(block, 0, len(block.content))]
    if counter(block.content) <= target:
        return [whole], []
    spans = _table_rows(block.content)
    if not spans:
        return [whole], [issue("gap", "表格行区间无法定位，保留完整表格", [block.block_id])]
    try:
        payload = json.loads(block.content)
    except ValueError:
        return [whole], [issue("gap", "表格 JSON 无法完整解析，保留原文", [block.block_id])]
    if not isinstance(payload, dict):
        return [whole], [issue("gap", "表格结构未知，保留原文", [block.block_id])]
    declared = payload.get("header_rows", [])
    # 仅接受从首行开始的连续显式标记；不使用非空列数、字体或首行位置猜表头。
    valid = (
        payload.get("header_source") == "docx_repeat_header"
        and isinstance(declared, list) and bool(declared)
        and all(type(i) is int for i in declared)
        and declared == list(range(len(declared))) and len(declared) <= len(spans)
    )
    headers = [_Piece(block, *spans[i]) for i in declared] if valid else []
    start = len(headers)
    data = [_Piece(block, a, b) for a, b in spans[start:]]
    if not data:
        return [headers], []
    problems = [] if headers else [issue(
        "gap", "表格表头未明确标记；按行分组但不重复首行，需核对后续组的列语义", [block.block_id]
    )]
    groups: list[list[_Piece]] = []
    pending = list(headers)
    for row in data:
        if len(pending) > len(headers) and counter(_text(pending + [row])) > target:
            groups.append(pending)
            pending = list(headers)
        pending.append(row)
    if pending:
        groups.append(pending)
    return groups, problems
