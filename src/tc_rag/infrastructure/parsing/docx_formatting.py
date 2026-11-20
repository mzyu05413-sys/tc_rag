"""直接run删除线映射；保留原始文本，JSON表格偏移按编码后字符计。"""

import json
from typing import Any

from docx.oxml.ns import qn
from docx.table import Table


def struck(node: Any) -> bool:
    run = node
    while run is not None and run.tag != qn("w:r"):
        run = run.getparent()
    props = run.find(qn("w:rPr")) if run is not None else None
    if props is None:
        return False
    return any(flag is not None and flag.get(qn("w:val"), "true").lower()
               not in {"0", "false", "off"}
               for flag in (props.find(qn("w:strike")), props.find(qn("w:dstrike"))))


def paragraph_ranges(element: Any, text: str) -> list[dict[str, int]]:
    """标题按原始文本映射；不以删除后的文本冒充来源。"""
    ranges = []
    offset = 0
    for node in element.iter():
        part = (node.text or "") if node.tag == qn("w:t") else (
            "\t" if node.tag == qn("w:tab") else
            "\n" if node.tag in {qn("w:br"), qn("w:cr")} else "")
        if part and struck(node):
            if text[offset:offset + len(part)] != part:
                raise ValueError("标题文本映射不一致")
            ranges.append({"start_char": offset, "end_char": offset + len(part)})
        offset += len(part)
    return ranges


def table_ranges(table: Table, payload: str) -> list[dict[str, int]]:
    ranges = []
    cursor = payload.index('"rows"')
    for row in table.rows:
        for cell in row.cells:
            value = cell.text
            encoded = json.dumps(value, ensure_ascii=False)
            position = payload.find(encoded, cursor)
            if position < 0:
                raise ValueError("表格原文与序列化位置不一致")
            cursor = position + len(encoded)
            offset = 0
            for index, paragraph in enumerate(cell.paragraphs):
                if index:
                    offset += 1
                for node in paragraph._p.iter():
                    part = (node.text or "") if node.tag == qn("w:t") else (
                        "\t" if node.tag == qn("w:tab") else
                        "\n" if node.tag in {qn("w:br"), qn("w:cr")} else "")
                    if part and struck(node):
                        if value[offset:offset+len(part)] != part:
                            raise ValueError("单元格文本映射不一致")
                        start = position + 1 + len(json.dumps(value[:offset], ensure_ascii=False))-2
                        end = position + 1 + len(json.dumps(
                            value[:offset+len(part)], ensure_ascii=False))-2
                        ranges.append({"start_char": start, "end_char": end})
                    offset += len(part)
    return ranges
