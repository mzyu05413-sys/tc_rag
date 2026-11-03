"""构造虚拟 Word/PDF，验证扩展结构解析及整文档页眉页脚隔离。"""

import json
from io import BytesIO

import pymupdf
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from tc_rag.application.ports.contracts import PageOutcome, ParseState
from tc_rag.application.services.chunking import structural_chunks
from tc_rag.infrastructure.parsing.docx import DocxParser
from tc_rag.infrastructure.parsing.layout_diagnostics import (
    docx_structure_statistics,
    isolate_running_blocks,
)
from tc_rag.infrastructure.parsing.pdf import PdfParser
from tc_rag.infrastructure.storage.files import LocalArtifactStore


def outline(element, level):
    properties = element.get_or_add_pPr()
    item = OxmlElement("w:outlineLvl")
    item.set(qn("w:val"), str(level))
    properties.append(item)


def control(document, elements, *, toc=False):
    sdt = OxmlElement("w:sdt")
    if toc:
        properties = OxmlElement("w:sdtPr")
        gallery = OxmlElement("w:docPartGallery")
        gallery.set(qn("w:val"), "Table of Contents")
        properties.append(gallery)
        sdt.append(properties)
    content = OxmlElement("w:sdtContent")
    for element in elements:
        content.append(element)
    sdt.append(content)
    document.element.body.insert(-1, sdt)
    return sdt


def save_word(document, tmp_path):
    stream = BytesIO()
    document.save(stream)
    store = LocalArtifactStore(tmp_path)
    return store, store.put("sample.docx", stream.getvalue()), stream.getvalue()


def test_outline_priority_inheritance_and_body_override(tmp_path):
    document = Document()
    heading = document.add_heading("Explicit", 1)
    outline(heading._p, 5)
    base = document.styles.add_style("CustomBase", WD_STYLE_TYPE.PARAGRAPH)
    outline(base.element, 1)
    child = document.styles.add_style("CustomChild", WD_STYLE_TYPE.PARAGRAPH)
    child.base_style = base
    document.add_paragraph("Inherited", style=child)
    plain = document.add_paragraph("Body", style=child)
    outline(plain._p, 9)
    local_style = document.styles.add_style("标题 3", WD_STYLE_TYPE.PARAGRAPH)
    document.add_paragraph("Local", style=local_style)
    store, asset, _ = save_word(document, tmp_path)
    result = DocxParser(store, extended=True).parse(asset)
    assert [b.kind for b in result.blocks] == ["heading", "heading", "paragraph", "heading"]
    assert result.blocks[1].heading_path == ["Explicit", "Inherited"]
    assert result.blocks[-1].heading_path == ["Explicit", "Inherited", "Local"]
    assert any("大纲级别" in issue.description for issue in result.issues)
    assert DocxParser(store, extended=False).parse(asset).blocks[1].kind == "paragraph"


def test_controls_expand_in_reading_order_with_real_locators_and_toc_skipped(tmp_path):
    document = Document()
    document.add_paragraph("Before")
    title = document.add_heading("Inside", 1)
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Cell"
    nested_text = document.add_paragraph("Nested")
    nested = control(document, [nested_text._p])
    control(document, [title._p, table._tbl, nested])
    directory = document.add_paragraph("Directory")
    control(document, [directory._p], toc=True)
    document.add_paragraph("After")
    store, asset, raw = save_word(document, tmp_path)
    result = DocxParser(store, extended=True).parse(asset)
    assert [b.kind for b in result.blocks] == [
        "paragraph",
        "heading",
        "table",
        "paragraph",
        "paragraph",
    ]
    assert [b.content for b in result.blocks if b.kind != "table"] == [
        "Before",
        "Inside",
        "Nested",
        "After",
    ]
    assert [b.locator.paragraph_index for b in result.blocks if b.kind != "table"] == [0, 1, 2, 3]
    assert result.blocks[2].locator.table_index == 0
    assert result.blocks[-1].heading_path == ["Inside"]
    assert any("跳过目录" in i.description for i in result.issues)
    assert DocxParser(store, extended=True).parse(asset).blocks == result.blocks
    old = DocxParser(store).parse(asset)
    assert [b.content for b in old.blocks] == ["Before", "After"]
    assert any("未支持元素" in i.description for i in old.issues)
    assert docx_structure_statistics(raw)["top_level_content_controls"] == 2


def test_malformed_control_is_reported_instead_of_silently_dropped(tmp_path):
    document = Document()
    document.add_paragraph("可读正文")
    document.element.body.insert(-1, OxmlElement("w:sdt"))
    store, asset, _ = save_word(document, tmp_path)
    parsed = DocxParser(store, extended=True).parse(asset)
    assert parsed.blocks[0].content == "可读正文"
    assert any("未支持元素" in issue.description for issue in parsed.issues)


def pdf_bytes():
    with pymupdf.open() as doc:
        for index in range(4):
            page = doc.new_page(height=800 if index % 2 == 0 else 1000)
            page.insert_text((40, 30), f"Header {index + 1}", fontsize=11)
            page.insert_text((40, 140), "Repeated business statement must remain in all pages.")
            page.insert_text((40, 190), "Business body has enough characters for extraction.")
            page.insert_text((40, page.rect.height - 20), str(index + 1), fontsize=11)
        return doc.tobytes()


def test_pdf_keep_default_isolate_preserves_source_and_checkpoint_finalization(tmp_path):
    store = LocalArtifactStore(tmp_path)
    asset = store.put("sample.pdf", pdf_bytes())
    original = PdfParser(store).parse(asset)
    assert PdfParser(store, running_text="keep").parse(asset).blocks == original.blocks
    parser = PdfParser(store, running_text="isolate")
    result = parser.parse(asset)
    assert len(result.blocks) == len(original.blocks)
    assert [b.content for b in result.blocks] == [b.content for b in original.blocks]
    assert [b.locator for b in result.blocks] == [b.locator for b in original.blocks]
    assert sum(b.kind == "other" for b in result.blocks) == 8
    assert all(b.kind != "other" for b in result.blocks if "business" in b.content)
    chunks = structural_chunks(result.blocks, 900, 0)
    assert not any("Header" in c.text for c in chunks.chunks)
    assert any("Repeated business" in c.text for c in chunks.chunks)
    # 模拟第一部分从检查点恢复，余下页继续解析，最终应按全文重新计算重复阈值。
    with parser.session(asset) as session:
        state = ParseState(heading_sizes=session.heading_sizes)
        checkpoints = []
        for number in range(2):
            page = session.parse_page(number, state)
            checkpoints.append(json.loads(json.dumps(page.to_payload())))
            state = page.state
    restored = []
    for payload in checkpoints:
        outcome = PageOutcome.from_payload(payload)
        restored.extend(outcome.blocks)
        state = outcome.state
    with parser.session(asset) as session:
        for number in range(2, 4):
            page = session.parse_page(number, state)
            restored.extend(page.blocks)
            state = page.state
        finalized, _ = session.finalize(restored)
    assert finalized == result.blocks
    assert any(i.severity == "info" and "隔离了 8 行" in i.description for i in result.issues)


def test_pdf_repetition_minimum_and_body_position_are_both_required(tmp_path):
    store = LocalArtifactStore(tmp_path)
    asset = store.put("sample.pdf", pdf_bytes())
    blocks = PdfParser(store).parse(asset).blocks
    two_pages = [b for b in blocks if b.locator.page <= 2]
    isolated, _ = isolate_running_blocks(two_pages, {1: 800, 2: 1000})
    assert all(b.kind != "other" for b in isolated if "Header" in b.content)
    assert sum(b.kind == "other" for b in isolated) == 2  # 纯页码依位置识别，无需三页重复。
