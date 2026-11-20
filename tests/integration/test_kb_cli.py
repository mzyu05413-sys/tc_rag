"""知识库正式入口的默认预览不装配真实模型。"""

from docx import Document
from typer.testing import CliRunner

from tc_rag.interfaces.cli import app


def test_preview_does_not_build_or_write_database(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    Document().save(source / "demo.docx")

    def forbidden(*args, **kwargs):
        raise AssertionError("预览不能装配模型")

    monkeypatch.setattr("tc_rag.bootstrap.build_kb_ingestion", forbidden)
    root = tmp_path / "runtime"
    result = CliRunner().invoke(app, [
        "kb", "ingest", "crm", "--source-dir", str(source),
        "--config", "config/defaults.yaml", "--root", str(root),
    ])
    assert result.exit_code == 0, result.output
    assert "demo.docx" in result.output
    assert not root.exists()
