"""用程序生成文字、表格和扫描页，验证PDF证据定位与未解析范围。"""

import json
from io import BytesIO

import pymupdf
import pytest
from PIL import Image

from tc_rag.application.ports.contracts import ParseState
from tc_rag.application.services.ingest import ingest_document
from tc_rag.infrastructure.parsing.pdf import PdfParser
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (50, 50), "blue").save(stream, format="PNG")
    return stream.getvalue()


def fixture_pdf(path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 40), "Demo heading", fontsize=20)
        page.insert_text(
            (40, 80), "Do not remove units: amount > 0 USD; keep negative conditions.", fontsize=11
        )
        page.insert_text(
            (40, 100),
            "Body paragraph provides enough ordinary text for font estimation.",
            fontsize=11,
        )
        # 有边框的二维小表格，供pdfplumber稳定发现候选。
        for x in (40, 200, 360):
            page.draw_line((x, 140), (x, 220))
        for y in (140, 180, 220):
            page.draw_line((40, y), (360, y))
        for x, y, value in [
            (50, 165, "Field"),
            (210, 165, "Constraint"),
            (50, 205, "Amount"),
            (210, 205, "> 0 USD"),
        ]:
            page.insert_text((x, y), value, fontsize=11)
        page.insert_image(pymupdf.Rect(40, 260, 90, 310), stream=image_bytes())
        scanned = doc.new_page()
        scanned.insert_image(pymupdf.Rect(40, 40, 400, 600), stream=image_bytes())
        doc.new_page()  # 空白页也必须明确报告，不能静默遗漏。
        doc.save(path)


def test_text_table_image_and_unsupported_pages(tmp_path):
    path = tmp_path / "demo.pdf"
    fixture_pdf(path)
    store = LocalArtifactStore(tmp_path / "managed")
    repo = SqliteRepository(tmp_path / "data.sqlite")
    asset = ingest_document(path, store, repo).asset
    parsed = PdfParser(store).parse(asset)
    assert parsed.blocks[0].kind == "heading"
    assert all(block.locator.page == 1 for block in parsed.blocks)
    assert all(block.locator.bbox is not None for block in parsed.blocks)
    assert [block.order for block in parsed.blocks] == list(range(len(parsed.blocks)))
    text = "\n".join(block.content for block in parsed.blocks if block.kind == "paragraph")
    assert "Do not remove units: amount > 0 USD" in text
    assert all(block.heading_path == ["Demo heading"] for block in parsed.blocks)
    tables = [block for block in parsed.blocks if block.kind == "table"]
    assert len(tables) == 1
    assert json.loads(tables[0].content)["rows"] == [["Field", "Constraint"], ["Amount", "> 0 USD"]]
    assert "Field" not in text  # 表格文字不会再作为独立正文重复输出。
    assert len(parsed.assets) == 1
    assert store.read(parsed.assets[0]).startswith(b"\x89PNG")
    assert any("第2页" in issue.description for issue in parsed.issues)
    assert any("第3页" in issue.description for issue in parsed.issues)
    assert any("图片文字未解析" in issue.description for issue in parsed.issues)
    assert PdfParser(store).parse(asset).blocks == parsed.blocks


def test_scan_policy_and_quality_threshold(tmp_path):
    store = LocalArtifactStore(tmp_path / "managed")
    with pytest.raises(ValueError, match="OCR"):
        PdfParser(store, scan_policy="ocr")
    with pytest.raises(ValueError):
        PdfParser(store, min_text_chars=0)
    path = tmp_path / "short.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 40), "Short title")
        doc.save(path)
    repo = SqliteRepository(tmp_path / "data.sqlite")
    asset = ingest_document(path, store, repo).asset
    assert PdfParser(store).parse(asset).blocks == []
    assert PdfParser(store, min_text_chars=5).parse(asset).blocks


def test_encrypted_and_corrupt_pdf(tmp_path):
    store = LocalArtifactStore(tmp_path / "managed")
    with pymupdf.open() as doc:
        doc.new_page()
        content = doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
        )
    asset = store.put("encrypted.pdf", content)
    with pytest.raises(ValueError, match="加密"):
        PdfParser(store).parse(asset)
    invalid = store.put("invalid.pdf", b"%PDF-1.7\nnot a document")
    with pytest.raises(pymupdf.FileDataError):
        PdfParser(store).parse(invalid)


def test_table_adapter_failure_preserves_text(tmp_path, monkeypatch):
    import tc_rag.infrastructure.parsing.pdf as module

    path = tmp_path / "demo.pdf"
    fixture_pdf(path)
    store = LocalArtifactStore(tmp_path / "managed")
    asset = store.put("demo.pdf", path.read_bytes())

    def fail(*args, **kwargs):
        raise RuntimeError("模拟表格解析不可用")

    monkeypatch.setattr(module.pdfplumber, "open", fail)
    result = PdfParser(store).parse(asset)
    assert any("Do not remove" in block.content for block in result.blocks)
    assert any("表格候选未提取" in issue.description for issue in result.issues)


def test_paged_session_matches_whole_document(tmp_path):
    """按页解析并传递状态，必须与整文档解析产出完全一致（含块顺序和标题路径）。"""
    path = tmp_path / "demo.pdf"
    fixture_pdf(path)
    store = LocalArtifactStore(tmp_path / "managed")
    repo = SqliteRepository(tmp_path / "data.sqlite")
    asset = ingest_document(path, store, repo).asset
    parser = PdfParser(store)

    whole = parser.parse(asset)
    with parser.session(asset) as session:
        state = ParseState(heading_sizes=list(session.heading_sizes))
        paged = []
        for page_index in range(session.page_count):
            outcome = session.parse_page(page_index, state)
            paged.extend(outcome.blocks)
            state = outcome.state

    assert paged == whole.blocks
    assert ParseState.from_payload(state.to_payload()) == state
