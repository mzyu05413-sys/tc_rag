"""CLI 质检命令：只读已完成运行，不创建模型客户端。"""

import json

import pytest
from typer.testing import CliRunner

from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces import workflow


@pytest.mark.asyncio
async def test_quality_cli_writes_reports_and_does_not_call_model(tmp_path, monkeypatch):
    from test_pipeline import setup

    pipeline, document, config_file, model = setup(tmp_path)
    await pipeline.generate(document, "生成测试点", tmp_path / "out", run_id="run_demo")
    calls_before = model.calls

    result = CliRunner().invoke(
        workflow.app,
        ["quality", "run_demo", "--config", str(config_file)],
    )

    assert result.exit_code == 0, result.output
    assert model.calls == calls_before
    report_dir = tmp_path / "out" / "run_demo" / "reports"
    report = json.loads((report_dir / "quality_check.json").read_text(encoding="utf-8"))
    assert report["run_id"] == "run_demo"
    assert report["semantic_review"] == "not_performed"
    assert (report_dir / "quality_check.md").is_file()
    assert report["overall"]["status"] in {"pass", "warn"}

    custom = tmp_path / "custom-reports"
    result = CliRunner().invoke(
        workflow.app,
        [
            "quality",
            "run_demo",
            "--config",
            str(config_file),
            "--output",
            str(custom),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (custom / "quality_check.json").is_file()
    assert (custom / "quality_check.md").is_file()


@pytest.mark.asyncio
async def test_quality_cli_returns_two_on_broken_snapshot(tmp_path):
    from test_pipeline import setup

    pipeline, document, config_file, _ = setup(tmp_path)
    await pipeline.generate(document, "生成测试点", tmp_path / "out", run_id="run_demo")
    repo = SqliteRepository(str(tmp_path / "data.sqlite"))
    key = "run_demo:extract"
    snapshot = repo.get("stage_snapshots", key, StageSnapshot)
    assert snapshot is not None
    snapshot.payload["evidence"][0]["quote"] = "原文不存在"
    repo.save("stage_snapshots", key, snapshot)

    result = CliRunner().invoke(
        workflow.app,
        ["quality", "run_demo", "--config", str(config_file)],
    )

    assert result.exit_code == 2, result.output
    report = json.loads(
        (tmp_path / "out" / "run_demo" / "reports" / "quality_check.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["overall"]["status"] == "fail"
    assert next(
        item for item in report["checks"] if item["check_id"] == "Q-EX-01"
    )["status"] == "fail"
