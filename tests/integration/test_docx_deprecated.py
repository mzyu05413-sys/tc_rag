"""删除线不进入有效检索证据，原始字符及偏移仍可审计。"""

import json
from io import BytesIO

import pytest
from docx import Document

from tc_rag.application.services.chunking import structural_chunks, verify_evidence
from tc_rag.domain.models import DocumentBlock, EvidenceSpan
from tc_rag.infrastructure.parsing.docx import DocxParser
from tc_rag.infrastructure.storage.files import LocalArtifactStore


def parse(doc, tmp_path):
    stream = BytesIO()
    doc.save(stream)
    store = LocalArtifactStore(tmp_path / "managed")
    return DocxParser(store).parse(store.put("sample.docx", stream.getvalue()))


def test_struck_paragraph_preserves_offsets_and_rejects_obsolete_quote(tmp_path):
    doc = Document()
    p = doc.add_paragraph("有效规则：")
    p.add_run("默认开启").font.strike = True
    p.add_run("用户手动设置")
    result = parse(doc, tmp_path)
    block = result.blocks[0]
    assert block.content == "有效规则：默认开启用户手动设置"
    assert [(r.start_char, r.end_char) for r in block.deprecated_ranges] == [(5, 9)]
    chunks = structural_chunks(result.blocks, 200, 0).chunks
    assert chunks[0].text == "有效规则：\n用户手动设置"
    assert chunks[0].text == "\n".join(
        block.content[s.start_char:s.end_char] for s in chunks[0].sources)
    for quote, valid in [("默认开启", False), ("用户手动设置", True),
                         ("有效规则：默认开启", False)]:
        evidence = EvidenceSpan(evidence_id="e", block_id=block.block_id,
                                locator=block.locator, quote=quote)
        assert verify_evidence(evidence, block) is valid
    assert DocumentBlock.model_validate_json(block.model_dump_json()) == block
    old = block.model_dump(exclude={"deprecated_ranges"})
    assert DocumentBlock.model_validate(old).deprecated_ranges == []


def test_table_strike_handles_json_escapes_and_keeps_live_values(tmp_path):
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "字段"
    p = table.cell(0, 1).paragraphs[0]
    p.add_run('旧"值\\路径\n下一行').font.double_strike = True
    p.add_run("现行规则")
    result = parse(doc, tmp_path)
    block = result.blocks[0]
    assert json.loads(block.content)["rows"][0][1].endswith("现行规则")
    removed = "".join(block.content[r.start_char:r.end_char]
                      for r in block.deprecated_ranges)
    assert removed == json.dumps('旧"值\\路径\n下一行', ensure_ascii=False)[1:-1]
    chunks = structural_chunks(result.blocks, 500, 0).chunks
    assert "现行规则" in chunks[0].text
    assert "旧" not in chunks[0].text and "下一行" not in chunks[0].text
    for chunk in chunks:
        assert chunk.text == "\n".join(block.content[s.start_char:s.end_char]
                                        for s in chunk.sources)


@pytest.mark.parametrize("heading", [False, True])
def test_fully_struck_text_is_not_indexed(tmp_path, heading):
    doc = Document()
    p = doc.add_heading("", level=1) if heading else doc.add_paragraph()
    p.add_run("全部作废").font.strike = True
    blocks = parse(doc, tmp_path).blocks
    assert blocks[0].content == "全部作废"
    assert structural_chunks(blocks, 100, 0).chunks == []


def test_explicit_false_is_retained(tmp_path):
    doc = Document()
    doc.add_paragraph().add_run("有效文字").font.strike = False
    blocks = parse(doc, tmp_path).blocks
    assert blocks[0].deprecated_ranges == []
    assert structural_chunks(blocks, 100, 0).chunks[0].text == "有效文字"
