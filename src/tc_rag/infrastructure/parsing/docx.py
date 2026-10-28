"""按 DOCX 主文档顺序解析文本、表格与图片，显式报告暂不支持的内容。"""

import hashlib
import json
import re
from io import BytesIO
from typing import Any, Literal

from docx import Document as OpenDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from tc_rag.application.ports.contracts import ArtifactStore, ParsedDocument
from tc_rag.domain.models import Asset, DocumentBlock, Issue, Locator


class DocxParser:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def parse(self, asset: Asset) -> ParsedDocument:
        """DOCX 不提供可靠排版页码，使用段落/表格/单元格索引定位。"""
        document = OpenDocument(BytesIO(self.store.read(asset)))
        result = ParsedDocument(blocks=[])
        document_id = "doc_" + asset.checksum
        headings: dict[int, str] = {}

        def add(
            kind: Literal["heading", "paragraph", "table", "image", "other"],
            text: str,
            locator: Locator,
        ) -> None:
            order = len(result.blocks)
            result.blocks.append(
                DocumentBlock(
                    block_id=f"{document_id}_block_{order}",
                    document_id=document_id,
                    kind=kind,
                    heading_path=[headings[level] for level in sorted(headings)],
                    order=order,
                    content=text,
                    locator=locator,
                )
            )

        def issue(description: str) -> None:
            result.issues.append(
                Issue(
                    issue_id=f"{document_id}_issue_{len(result.issues)}",
                    kind="unparsed",
                    severity="warning",
                    object_ids=[document_id],
                    description=description,
                )
            )

        def paragraph(element: Any, locator: Locator, include_text: bool = True) -> None:
            # 遍历XML内容而不是只用paragraph.runs，保留超链接中的文字和图片锚点顺序。
            pending: list[str] = []

            def flush() -> None:
                if pending and include_text:
                    add("paragraph", "".join(pending), locator)
                pending.clear()

            for node in element.iter():
                if node.tag == qn("w:t"):
                    pending.append(node.text or "")
                elif node.tag == qn("w:tab"):
                    pending.append("\t")
                elif node.tag == qn("w:br"):
                    pending.append("\n")
                elif node.tag == qn("a:blip"):
                    flush()
                    relationship = node.get(qn("r:embed"))
                    if not relationship or relationship not in document.part.related_parts:
                        issue(f"位置 {locator.model_dump_json()} 的外链或缺失图片未解析")
                        continue
                    part = document.part.related_parts[relationship]
                    content = part.blob
                    checksum = hashlib.sha256(content).hexdigest()
                    suffix = str(part.partname).rsplit(".", 1)[-1]
                    image = self.store.put(
                        f"assets/{asset.checksum}/images/{checksum}.{suffix}", content
                    )
                    result.assets.append(image)
                    add("image", json.dumps(image.model_dump(), ensure_ascii=False), locator)
                    issue(f"图片 {image.asset_id} 仅提取与定位，图片文字尚未解析")
                elif node.tag in {qn("w:txbxContent"), qn("w:object"), qn("w:pict")}:
                    issue(
                        f"位置 {locator.model_dump_json()} 含文本框或嵌入对象，布局/内容可能不完整"
                    )
            flush()

        paragraph_index = table_index = 0
        for child in document.element.body:
            if child.tag == qn("w:p"):
                value = Paragraph(child, document)
                style = value.style.name if value.style else ""
                heading = re.fullmatch(r"Heading (\d+)", style)
                locator = Locator(paragraph_index=paragraph_index)
                if heading:
                    level = int(heading.group(1))
                    headings = {key: text for key, text in headings.items() if key < level}
                    headings[level] = value.text
                    add("heading", value.text, locator)
                    paragraph(child, locator, include_text=False)
                else:
                    paragraph(child, locator)
                paragraph_index += 1
            elif child.tag == qn("w:tbl"):
                table = Table(child, document)
                # 保留二维行列而不扁平拼接；合并单元格的重复值不当成独立业务事实。
                rows = [[cell.text for cell in row.cells] for row in table.rows]
                add(
                    "table",
                    json.dumps({"schema_version": "1.0", "rows": rows}, ensure_ascii=False),
                    Locator(table_index=table_index),
                )
                if child.xpath(".//w:gridSpan | .//w:vMerge"):
                    issue(f"表格 {table_index} 含合并单元格，行列语义需要人工复核")
                for row_index, row in enumerate(table.rows):
                    seen = set()
                    for column_index, cell in enumerate(row.cells):
                        if cell._tc in seen:
                            continue
                        seen.add(cell._tc)
                        if cell.tables:
                            issue(f"表格 {table_index} 单元格含嵌套表格，未展开")
                        for p in cell.paragraphs:
                            paragraph(
                                p._p,
                                Locator(
                                    table_index=table_index,
                                    row_index=row_index,
                                    column_index=column_index,
                                ),
                                include_text=False,
                            )
                table_index += 1
            elif child.tag != qn("w:sectPr"):
                issue(f"主文档含未支持元素 {child.tag}，未自动展开")
        for part in document.part.package.parts:
            name = str(part.partname)
            if any(key in name for key in ("/header", "/footer", "/footnotes", "/endnotes")):
                issue(f"附属内容 {name} 尚未解析")
        if not result.blocks:
            issue("主文档没有可解析正文")
        return result
