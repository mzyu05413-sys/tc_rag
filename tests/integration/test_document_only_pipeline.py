"""删除参考分支后仍通过需求两轮 RAG 连续交付，并保留历史记录查询。"""

from copy import deepcopy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tc_rag.domain.models import Run
from tc_rag.interfaces.workflow import app


@pytest.mark.asyncio
async def test_document_rag_stays_continuous_without_reference_branch(tmp_path):
    from test_pipeline import FixtureLLM, setup

    class RecordingLLM(FixtureLLM):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def generate(self, request, output_schema):
            self.requests.append(deepcopy(request))
            return await super().generate(request, output_schema)

    pipeline, document, config, _ = setup(tmp_path)
    pipeline.llm = model = RecordingLLM()
    events = []
    pipeline.progress = events.append
    result = await pipeline.generate(document, "生成边界用例", tmp_path / "out")
    assert result["cases"] == 1
    assert Path(result["xmind"]).is_file()
    assert Path(result["excel"]).is_file()
    assert [event.stage for event in events if event.kind == "stage"] == [
        "ingest", "parse", "chunk", "extract", "index", "plan_points",
        "points_ready", "index", "generate", "validate", "export_cases", "completed",
    ]
    assert {request["stage"] for request in model.requests} == {
        "extract", "plan_points", "generate"
    }
    for request in model.requests:
        assert "code_context" not in request and "ui_context" not in request
        if request["stage"] in {"plan_points", "generate"}:
            assert "supplemental_evidence" in request and request["evidence"]
    run_id = result["run_id"]
    assert "sources" not in pipeline._run(run_id).config_snapshot
    assert "source_context" not in pipeline.manifest(run_id)
    bundles = pipeline.read(run_id, "retrieval")["bundles"]
    assert {bundle["generation"]["stage"] for bundle in bundles} == {"plan_points", "generate"}
    assert all("code_adopted_ids" not in bundle and "code_omitted_ids" not in bundle
               for bundle in bundles)

    # 历史配置字典不迁移、不复写：只读命令和已有事实的离线重导仍可使用。
    run = pipeline._run(run_id)
    historical = Run.model_validate({
        **run.model_dump(mode="json"),
        "config_snapshot": {**run.config_snapshot, "sources": {"enabled": True}},
    })
    pipeline.repository.save("runs", run_id, historical)
    runner = CliRunner()
    for arguments in (
        ["status", run_id],
        ["inspect", run_id, "--stage", "cases"],
        [
            "inspect",
            run_id,
            "--stage",
            "cases",
            "--format",
            "xlsx",
            "--output",
            str(tmp_path / "restored" / "cases.xlsx"),
        ],
    ):
        checked = runner.invoke(app, [*arguments, "--config", str(config)])
        assert checked.exit_code == 0, checked.output
    assert pipeline._run(run_id).config_snapshot["sources"] == {"enabled": True}
