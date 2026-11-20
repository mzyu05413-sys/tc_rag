"""离线材料准备使用正式解析器，禁止覆盖已有审核材料。"""

import json

import pytest
from docx import Document

from eval.kb.prepare_corpus import prepare


def test_prepare_preserves_text_and_rejects_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    document = Document()
    document.add_paragraph("审批条件：所有成员同意。")
    document.save(source / "demo.docx")
    output = tmp_path / "prepared"
    manifest = prepare(source, output)
    entry = manifest["documents"][0]
    blocks = json.loads((output / entry["sha256"] / "blocks.json").read_text("utf-8"))
    assert any(b["content"] == "审批条件：所有成员同意。" for b in blocks)
    assert entry["version_review"] == "pending"
    original = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, output)
    assert (output / "manifest.json").read_bytes() == original


def test_empty_source_does_not_publish(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    output = tmp_path / "prepared"
    with pytest.raises(ValueError):
        prepare(source, output)
    assert not output.exists()
