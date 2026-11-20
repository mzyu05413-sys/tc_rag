"""知识库终端样式与 RAG 保持一致，测试只用离线资料。"""

import asyncio
import json
from contextlib import asynccontextmanager

from test_kb_ingestion import setup
from typer.testing import CliRunner

from tc_rag.application.kb.search import SearchService
from tc_rag.interfaces.cli import app
from tc_rag.interfaces.kb.presentation import show_answer


def test_workbench_preview_never_constructs_model(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("预览不能创建模型")

    monkeypatch.setattr("tc_rag.bootstrap.kb_session", forbidden)
    result = CliRunner().invoke(app, [
        "kb", "start", "--config", "config/defaults.yaml", "--root", str(tmp_path / "kb"),
    ], input="2\ncordys\n会签条件\n")
    assert result.exit_code == 0, result.output
    assert "知识库工作台" in result.output
    assert "预览" in result.output
    assert not (tmp_path / "kb").exists()


def test_answer_is_readable_and_preserves_citation(capsys):
    show_answer({
        "answer": {"status": "answered", "claims": [{
            "text": "会签需要全员同意。", "citations": [{"chunk_id": "c", "quote": "原文"}],
        }]}, "evidence": [{"chunk_id": "c", "text": "原文", "sources": [{
            "document_id": "审批流.docx", "locator": {"paragraph_index": 2},
        }]}], "total_seconds": 0.2, "run_id": "a",
    })
    output = capsys.readouterr().out
    assert "[回答]" in output
    assert "会签需要全员同意。 [1]" in output
    assert "审批流.docx · 段落 3" in output
    assert '"claims"' not in output


def test_search_json_stdout_stays_machine_readable(tmp_path, monkeypatch):
    components, _, _, paths = setup(tmp_path)
    asyncio.run(components.ingest("crm", paths))

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield SearchService(components), None

    monkeypatch.setattr("tc_rag.bootstrap.kb_session", session)
    result = CliRunner().invoke(app, [
        "kb", "search", "crm", "-q", "会签", "--live", "--json",
        "--config", "config/defaults.yaml", "--root", str(tmp_path / "runtime"),
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["candidates"]
    assert "[阶段]" in result.stderr
    assert "检索知识库证据" in result.stderr
