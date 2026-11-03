"""原文结构诊断与 PDF 边缘文本候选；不执行 OCR 或多栏重排。"""

import re
from collections import defaultdict
from io import BytesIO
from math import ceil
from typing import Any

import pymupdf
from docx import Document
from docx.oxml.ns import qn

from tc_rag.domain.models import DocumentBlock, Issue


def normalized_running_text(text: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"\d+", "#", text))


def isolate_running_blocks(
    blocks: list[DocumentBlock], page_heights: dict[int, float]
) -> tuple[list[DocumentBlock], list[Issue]]:
    """仅在真实页高的边缘带及跨页重复共同成立时隔离，绝不删除原文。"""
    candidates: dict[str, tuple[str, str]] = {}
    pages_by_text: dict[tuple[str, str], set[int]] = defaultdict(set)
    text_pages = {
        block.locator.page
        for block in blocks
        if block.kind in {"heading", "paragraph", "table"}
        and block.content.strip()
        and block.locator.page is not None
    }
    page_number = re.compile(r"(?:第\s*\d+\s*页|\d+\s*/\s*\d+|[-–—]\s*\d+\s*[-–—]|\d+)")
    for block in blocks:
        page, box = block.locator.page, block.locator.bbox
        if block.kind not in {"paragraph", "heading"} or page is None or box is None:
            continue
        height = page_heights.get(page)
        if not height:
            continue
        side = "top" if box[3] <= height * 0.08 else "bottom" if box[1] >= height * 0.92 else None
        if side is None:
            continue
        key = side, normalized_running_text(block.content)
        candidates[block.block_id] = key
        pages_by_text[key].add(page)
    minimum = max(3, ceil(len(text_pages) * 0.5))
    isolated = [
        block
        for block in blocks
        if block.block_id in candidates
        and (
            len(pages_by_text[candidates[block.block_id]]) >= minimum
            or page_number.fullmatch(block.content.strip())
        )
    ]
    ids = {block.block_id for block in isolated}
    output = [
        block.model_copy(update={"kind": "other"}) if block.block_id in ids else block
        for block in blocks
    ]
    if not isolated:
        return output, []
    samples = "；".join(block.content[:20] for block in isolated[:3])
    return output, [
        Issue(
            issue_id=isolated[0].document_id + "_running_text",
            kind="validation",
            severity="info",
            object_ids=[block.block_id for block in isolated],
            description=(
                f"隔离了 {len(isolated)} 行疑似页眉页脚（重复出现在页面上下边缘）；样例：{samples}"
            ),
        )
    ]


def pdf_layout_statistics(content: bytes) -> dict[str, Any]:
    repeated: dict[tuple[str, str], set[int]] = defaultdict(set)
    multi_column = []
    with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
        for number, page in enumerate(document, 1):
            lines = [
                line for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
            ]
            bands: dict[int, list[tuple[float, float]]] = defaultdict(list)
            for line in lines:
                text = "".join(span["text"] for span in line["spans"])
                if not text.strip():
                    continue
                x0, y0, x1, y1 = line["bbox"]
                side = (
                    "top"
                    if y1 <= page.rect.height * 0.08
                    else ("bottom" if y0 >= page.rect.height * 0.92 else None)
                )
                if side:
                    repeated[(side, normalized_running_text(text))].add(number)
                else:
                    bands[int(y0 // 20)].append((x0, x1))
            # 至少三条水平带同时有中线左/右两簇才提示；表格也可能触发，不能当结论。
            matches = sum(
                any(end < page.rect.width * 0.48 for _, end in items)
                and any(start > page.rect.width * 0.52 for start, _ in items)
                for items in bands.values()
            )
            if matches >= 3:
                multi_column.append(number)
        pages = len(document)
    return {
        "pages": pages,
        "edge_ratio": 0.08,
        "running_text_top10": [
            {"position": side, "normalized_text": text, "pages": sorted(found), "count": len(found)}
            for (side, text), found in sorted(repeated.items(), key=lambda item: -len(item[1]))[:10]
        ],
        "possible_multicolumn_pages": multi_column,
        "warning": "边缘重复和左右两簇仅为候选诊断，表格可能误报；没有删除文字或执行阅读顺序重排。",
    }


def docx_structure_statistics(content: bytes) -> dict[str, int]:
    document = Document(BytesIO(content))
    paragraphs = document.element.body.xpath(".//w:p")
    return {
        "paragraphs": len(paragraphs),
        "numbered_paragraphs": sum(bool(p.xpath("./w:pPr/w:numPr")) for p in paragraphs),
        "outline_paragraphs": sum(bool(p.xpath("./w:pPr/w:outlineLvl")) for p in paragraphs),
        "top_level_content_controls": sum(c.tag == qn("w:sdt") for c in document.element.body),
        "textboxes": len(document.element.body.xpath(".//w:txbxContent")),
    }
