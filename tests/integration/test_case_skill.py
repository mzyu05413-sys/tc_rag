"""用例 Skill 仅进入生成请求，不能成为需求证据或改变输出格式。"""

import json

import pytest
from typer.testing import CliRunner

from tc_rag.application.ports.errors import StartupConfigurationError
from tc_rag.application.requests import CasesRequest
from tc_rag.bootstrap import build_pipeline
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces import workflow


@pytest.mark.parametrize("entry", ["run", "start"])
def test_custom_skill_is_used_only_for_case_generation(tmp_path, monkeypatch, entry):
    from test_pipeline import FixtureLLM, setup

    original, document, config, _ = setup(tmp_path)
    original.config.models.llm.model = "fixture"
    config.write_text(original.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture")
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture")
    skill = tmp_path / "style.md"
    marker = "custom-style-sentinel"
    skill.write_text("# 用例规范\n" + marker, encoding="utf-8")
    seen = []

    class Spy(FixtureLLM):
        async def generate(self, request, schema):
            seen.append(request)
            return await super().generate(request, schema)

    monkeypatch.setattr(
        workflow,
        "build_pipeline",
        lambda cfg, run_id: build_pipeline(
            cfg,
            run_id,
            Spy(),
            FakeEmbedder(),
        ),
    )
    args = [entry]
    if entry == "run":
        args += [str(document), "-i", "生成用例"]
    args += [
        "--config",
        str(config),
        "--output-dir",
        str(tmp_path / "out"),
        "--skill-file",
        str(skill),
    ]
    result = CliRunner().invoke(workflow.app, args, input=f"{document}\n生成用例\n")
    assert result.exit_code == 0, result.output
    assert {r["stage"] for r in seen} == {"extract", "plan_points", "generate"}
    for request in seen:
        assert (marker in request["system"]) is (request["stage"] == "generate")
        if request["stage"] == "generate":
            assert "用例生成 Skill" in request["system"]
            assert marker not in json.dumps(request["evidence"], ensure_ascii=False)
    run_id = result.output.split("run_id=")[1].splitlines()[0]
    repo = SqliteRepository(original.config.storage.database)
    snapshot = repo.get("stage_snapshots", f"{run_id}:skill", StageSnapshot)
    assert snapshot.payload["builtin_checksum"] and snapshot.payload["custom_checksum"]
    assert marker not in snapshot.model_dump_json()
    assert list((tmp_path / "out" / run_id).glob("testpoints*.xmind"))
    assert list((tmp_path / "out" / run_id).glob("testcases*.xlsx"))


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid_utf8", "zip", "oversized"])
def test_invalid_skill_is_rejected_before_any_model_request(tmp_path, kind):
    from test_pipeline import FixtureLLM, setup

    original, _, _, _ = setup(tmp_path)
    file = tmp_path / ("skill.zip" if kind == "zip" else "skill.md")
    if kind != "missing":
        file.write_bytes(
            {
                "empty": b" ",
                "invalid_utf8": b"\xff",
                "zip": b"PK",
                "oversized": b"x" * (256 * 1024 + 1),
            }[kind]
        )
    original.config.generation.skill_file = str(file)
    model = FixtureLLM()
    with pytest.raises(StartupConfigurationError, match="generation.skill_file"):
        build_pipeline(original.config, "run", model, FakeEmbedder())
    assert model.calls == 0


@pytest.mark.asyncio
async def test_changing_skill_content_invalidates_only_generation_cache(tmp_path):
    from test_pipeline import FixtureLLM, setup

    original, document, _, _ = setup(tmp_path)
    skill = tmp_path / "style.md"
    skill.write_text("# 风格甲", encoding="utf-8")
    original.config.generation.skill_file = str(skill)
    model = FixtureLLM()
    pipeline = build_pipeline(original.config, "run_demo", model, FakeEmbedder())
    await pipeline.generate(document, "生成用例", tmp_path / "out", run_id="run_demo")
    first = pipeline.read("run_demo", "skill")["custom_checksum"]
    assert model.calls == 3
    skill.write_text("# 风格乙", encoding="utf-8")
    rerun = build_pipeline(original.config, "run_demo", model, FakeEmbedder())
    await rerun.cases(
        CasesRequest(
            run_id="run_demo",
            instruction="生成用例",
            output_dir=tmp_path / "out",
        )
    )
    assert model.calls == 4  # 只重做用例生成，不复用旧 Skill 的生成结果。
    assert rerun.read("run_demo", "skill")["custom_checksum"] != first
