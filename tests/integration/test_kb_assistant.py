"""问答助手终端入口：预览不装配模型，实际对话只输出流式答案。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from tc_rag.infrastructure.providers.fakes import FakeStreamingLLM
from tc_rag.interfaces.cli import app


class _Repository:
    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}

    def save_result(self, run_id: str, result: dict[str, Any]) -> None:
        self.results[run_id] = result

    def active(self, kb_id: str) -> Any:
        return SimpleNamespace(snapshot=SimpleNamespace(snapshot_id="s1"))


class _Components:
    def __init__(self) -> None:
        self.repository = _Repository()
        self.runtime_identity = {"config": "test"}
        self.counter = len


class _Search:
    def __init__(self) -> None:
        self.components = _Components()

    async def search(
        self, kb_id: str, query: str, *, top_k: int = 5,
        document_ids: set[str] | None = None, snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": "search_1", "kb_id": kb_id, "snapshot_id": "s1",
            "document_ids": None, "query": query,
            "candidates": [{"chunk_id": "c1", "text": "会签需全员同意。", "sources": []}],
            "timings": {"total_seconds": 0.0},
        }


def test_preview_does_not_build_or_write_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("预览不能装配模型")

    monkeypatch.setattr("tc_rag.bootstrap.kb_session", forbidden)
    result = CliRunner().invoke(app, [
        "kb", "assistant", "cordys", "--config", "config/defaults.yaml",
        "--root", str(tmp_path / "kb"),
    ])
    assert result.exit_code == 0, result.output
    assert "预览" in result.output
    assert not (tmp_path / "kb").exists()


def test_live_stream_prints_answer_without_stage_or_sources(tmp_path, monkeypatch):
    @asynccontextmanager
    async def session(*args, **kwargs):
        yield _Search(), FakeStreamingLLM([["会签需", "全员同意。"]])

    monkeypatch.setattr("tc_rag.bootstrap.kb_session", session)
    result = CliRunner().invoke(app, [
        "kb", "assistant", "cordys", "--live",
        "--config", "config/defaults.yaml", "--root", str(tmp_path / "kb"),
    ], input="会签规则？\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "知识库：cordys" in result.output
    assert "回答模型：" in result.output
    assert "知识库快照：s1" in result.output
    assert "回答提示词：kb_qa/assistant_v5.md" in result.output
    assert "我是一个测试用例生成相关的AI问答助手，您有哪些疑问？" in result.output
    assert "会签需全员同意。" in result.output
    assert "[流程]" not in result.output
    assert "[证据来源]" not in result.output
