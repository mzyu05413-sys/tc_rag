"""交互入口的离线验证：真实解析和脑图导出，模型使用明确的测试夹具。"""

import pytest
from typer.testing import CliRunner

from tc_rag.bootstrap import build_pipeline
from tc_rag.domain.models import Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces import workflow


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("quiet", [False, True])
def test_start_generates_xmind_and_excel_together(tmp_path, monkeypatch, retry, quiet):
    from test_pipeline import FixtureLLM, setup

    pipeline, document, config, _ = setup(tmp_path)
    pipeline.config.models.llm.model = "fixture-llm"
    config.write_text(pipeline.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-key-not-a-real-secret")
    # 模拟含空格的文件地址，保留完整路径，不让 shell 拆分用户输入。
    document = document.rename(tmp_path / "requirement with spaces.docx")
    model = FixtureLLM()
    monkeypatch.setattr(
        workflow,
        "build_pipeline",
        lambda cfg, run_id: build_pipeline(cfg, run_id, model, FakeEmbedder()),
    )
    prefix = f"{tmp_path}\n{tmp_path / 'missing.docx'}\n" if retry else ""
    result = CliRunner().invoke(
        workflow.app,
        ["start", "--config", str(config), "--output-dir", str(tmp_path / "out")]
        + (["--quiet"] if quiet else []),
        input=prefix + f'"{document}"\n',
    )
    assert result.exit_code == 0, result.output
    run_id = result.output.split("run_id=")[1].splitlines()[0]
    assert list((tmp_path / "out" / run_id).glob("testpoints*.xmind"))
    repo = SqliteRepository(pipeline.config.storage.database)
    snapshot = repo.get("stage_snapshots", f"{run_id}:input", StageSnapshot)
    assert snapshot is not None
    expected_instruction = workflow.DEFAULT_INSTRUCTION
    assert snapshot.payload["instruction"] == expected_instruction
    assert "生成目标" in result.output
    assert model.calls == 3
    assert list((tmp_path / "out" / run_id).glob("testcases*.xlsx"))
    assert "人工审核" not in result.output and "Excel 测试用例" in result.output
    assert ("[阶段]" in result.stderr) is (not quiet)
    assert "[阶段]" not in result.stdout
    if not quiet:
        assert "[阶段] 01" in result.stderr and "接入文件" in result.stderr
        assert "RAG 检索" in result.stderr and "重排：配置关闭" in result.stderr
        assert "[阶段汇总] 流程完成" in result.stderr
        assert "联合导出 XMind、Excel 与报告" in result.stderr
    else:
        assert "[阶段汇总]" not in result.stderr
    if retry:
        assert result.output.count("文件不存在或格式未启用") == 2


def test_start_preview_always_includes_retrieval_without_model(tmp_path, monkeypatch):
    document, config = tmp_path / "preview.pdf", tmp_path / "config.yaml"
    document.touch()
    config.write_text("{}", encoding="utf-8")

    def no_model(*args, **kwargs):
        pytest.fail("预览不得创建模型客户端")

    monkeypatch.setattr(workflow, "build_pipeline", no_model)
    result = CliRunner().invoke(
        workflow.app,
        ["start", "--config", str(config), "--preview"],
        input=f"{document}\n",
    )
    assert result.exit_code == 0, result.output
    assert "RAG 唯一流程" in result.output
    assert "embed" in result.output


def test_start_cancel_does_not_create_pipeline(tmp_path, monkeypatch):
    document, config = tmp_path / "cancel.pdf", tmp_path / "config.yaml"
    document.touch()
    config.write_text(
        "models:\n  llm:\n    model: fixture-llm\n  embedding:\n    model: fixture-embedding\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-key-not-a-real-secret")

    def no_model(*args, **kwargs):
        pytest.fail("用户输入未完成，不得创建模型客户端")

    monkeypatch.setattr(workflow, "build_pipeline", no_model)
    result = CliRunner().invoke(
        workflow.app,
        ["start", "--config", str(config)],
        input="",
    )
    assert result.exit_code == 130
    assert "已取消输入，未开始生成" in result.output
    assert "执行失败" not in result.output
    assert "run_id=" not in result.output


@pytest.mark.parametrize("key", [None, "", "   "])
def test_start_missing_key_is_actionable_before_prompt(tmp_path, monkeypatch, key):
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  llm:\n    model: fixture-llm\n  embedding:\n    model: fixture-embedding\n",
        encoding="utf-8",
    )
    if key is None:
        monkeypatch.delenv("TC_RAG_LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TC_RAG_LLM_API_KEY", key)
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")

    def no_model(*args, **kwargs):
        pytest.fail("缺少密钥不得创建模型客户端")

    monkeypatch.setattr(workflow, "build_pipeline", no_model)
    result = CliRunner().invoke(workflow.app, ["start", "--config", str(config)])
    assert result.exit_code == 2
    assert "TC_RAG_LLM_API_KEY" in result.output
    assert "同一个 PowerShell" in result.output
    assert "需求文档路径" not in result.output


def test_start_requires_embedding_before_prompt(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  llm:\n    model: fixture-llm\n", encoding="utf-8")
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-key-not-a-real-secret")
    result = CliRunner().invoke(workflow.app, ["start", "--config", str(config)])
    assert result.exit_code == 2
    assert "models.embedding" in result.output
    assert "需求文档路径" not in result.output
    assert "fixture-key-not-a-real-secret" not in result.output


def test_unknown_error_does_not_expose_exception_body(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")

    def third_party_error(*args, **kwargs):
        raise ValueError("secret-sentinel-from-third-party")

    monkeypatch.setattr(workflow, "validate_runtime_config", third_party_error)
    result = CliRunner().invoke(workflow.app, ["start", "--config", str(config)])
    assert result.exit_code == 2
    assert "secret-sentinel-from-third-party" not in result.output
    assert "ValueError" in result.output


def test_provider_status_code_is_visible(tmp_path, monkeypatch):
    from tc_rag.infrastructure.providers.openai_sdk import ProviderError

    document = tmp_path / "fixture.pdf"
    document.touch()

    def bad_request(*args, **kwargs):
        raise ProviderError("HTTP_400")

    monkeypatch.setattr(workflow, "build_pipeline", bad_request)
    result = CliRunner().invoke(workflow.app, ["run", str(document), "-i", "生成测试点"])
    assert result.exit_code == 2
    assert "模型调用失败：HTTP_400" in result.output
    assert "response_format" in result.output


def test_embedding_balance_error_explains_why_rag_stops(tmp_path, monkeypatch):
    from tc_rag.application.ports.errors import EmbeddingProviderError

    document = tmp_path / "fixture.pdf"
    document.touch()

    def balance_error(*args, **kwargs):
        raise EmbeddingProviderError("HTTP_429_BIGMODEL_1113")

    monkeypatch.setattr(workflow, "build_pipeline", balance_error)
    result = CliRunner().invoke(workflow.app, ["run", str(document), "-i", "生成用例"])
    assert result.exit_code == 2
    assert "向量模型调用失败" in result.output
    assert "1113" in result.output and "欠费" in result.output
    assert "RAG 索引未完成" in result.output
    assert "RuntimeError" not in result.output


def test_generation_balance_error_shows_actionable_hint(tmp_path, monkeypatch):
    from tc_rag.infrastructure.providers.openai_sdk import ProviderError

    document = tmp_path / "fixture.pdf"
    document.touch()

    def balance_error(*args, **kwargs):
        raise ProviderError("HTTP_402")

    monkeypatch.setattr(workflow, "build_pipeline", balance_error)
    result = CliRunner().invoke(workflow.app, ["run", str(document), "-i", "生成用例"])
    assert result.exit_code == 2
    assert "HTTP_402" in result.output
    assert "余额不足" in result.output and "充值" in result.output


def test_context_budget_error_shows_counts_and_config_field(tmp_path, monkeypatch):
    from tc_rag.application.ports.errors import ContextBudgetError

    document = tmp_path / "fixture.pdf"
    document.touch()

    def too_large(*args, **kwargs):
        raise ContextBudgetError(12072, 12000)

    monkeypatch.setattr(workflow, "build_pipeline", too_large)
    result = CliRunner().invoke(workflow.app, ["run", str(document), "-i", "生成测试点"])
    assert result.exit_code == 2
    assert "上下文预算不足" in result.output
    assert "12072" in result.output and "12000" in result.output
    assert "runtime.max_context_tokens" in result.output


def test_doctor_offline_does_not_construct_pipeline(tmp_path, monkeypatch):
    config = tmp_path / "local.yaml"
    config.write_text(
        "models:\n  llm:\n    model: fixture-llm\n  embedding:\n    model: fixture-embedding\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-secret-do-not-print")

    def no_pipeline(*args, **kwargs):
        pytest.fail("离线诊断不得创建客户端或运行目录")

    monkeypatch.setattr(workflow, "build_pipeline", no_pipeline)
    result = CliRunner().invoke(workflow.app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "未调用模型" in result.output
    assert "fixture-secret-do-not-print" not in result.output
    assert list(tmp_path.iterdir()) == [config]


def test_doctor_live_isolates_business_data_and_limits_requests(tmp_path, monkeypatch):
    from test_pipeline import FixtureLLM, setup

    pipeline, _, config, _ = setup(tmp_path)
    pipeline.config.project.workdir = str(tmp_path / "private-work")
    pipeline.config.models.llm.model = "fixture-model"
    config.write_text(pipeline.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-secret-do-not-print")
    model, captured = FixtureLLM(), []

    def build(cfg, run_id):
        captured.append(cfg)
        return build_pipeline(cfg, run_id, model, FakeEmbedder())

    monkeypatch.setattr(workflow, "build_pipeline", build)
    result = CliRunner().invoke(workflow.app, ["doctor", "--live", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "RAG 检查通过" in result.output
    assert model.calls == 2
    assert "fixture-secret-do-not-print" not in result.output
    cfg = captured[0]
    assert cfg.runtime.budget.max_calls == 8
    assert cfg.runtime.max_attempts == 1
    assert cfg.generation.schema_repair_attempts == 0
    assert "llm_supplement" not in cfg.planning.model_dump()
    assert cfg.storage.database != pipeline.config.storage.database
    folder = list((tmp_path / "private-work" / "diagnostics").iterdir())[0]
    assert list(folder.rglob("testpoints*.xmind"))
    assert not list(folder.rglob("testcases*.xlsx"))
    repo = SqliteRepository(cfg.storage.database)
    retrieval = repo.get("stage_snapshots", f"{folder.name}:retrieval", StageSnapshot)
    assert retrieval.payload["bundles"]
    run = repo.get("runs", folder.name, Run)
    assert run.status == "succeeded"
    progress = repo.get("stage_snapshots", f"{folder.name}:progress", StageSnapshot)
    assert progress.payload["current_stage"] == "points_ready"


def test_automatic_config_prefers_local_then_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workdir").mkdir()
    (tmp_path / "config").mkdir()
    local, defaults = tmp_path / "workdir/local.yaml", tmp_path / "config/defaults.yaml"
    defaults.write_text("retrieval:\n  vector_top_k: 5\n", encoding="utf-8")
    assert workflow._config(None).retrieval.vector_top_k == 5
    local.write_text("retrieval:\n  vector_top_k: 9\n", encoding="utf-8")
    assert workflow._config(None).retrieval.vector_top_k == 9
    assert workflow._config(defaults).retrieval.vector_top_k == 5


def test_thinking_switch_is_rejected_for_responses_mode(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  llm:\n    model: fixture\n"
        "    api_mode: responses\n    thinking_mode: disabled\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-key")
    result = CliRunner().invoke(workflow.app, ["doctor", "--config", str(config)])
    assert result.exit_code == 2
    assert "thinking_mode" in result.output
    assert "Chat" in result.output


def test_doctor_live_empty_extraction_does_not_claim_success(tmp_path, monkeypatch):
    from test_pipeline import FixtureLLM, setup

    from tc_rag.application.ports.contracts import ModelResult

    pipeline, _, config, _ = setup(tmp_path)
    pipeline.config.project.workdir = str(tmp_path / "diagnostic-work")
    pipeline.config.models.llm.model = "fixture-model"
    config.write_text(pipeline.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("TC_RAG_EMBEDDING_API_KEY", "fixture-embedding-key")
    monkeypatch.setenv("TC_RAG_LLM_API_KEY", "fixture-key")

    class EmptyLLM(FixtureLLM):
        async def generate(self, request, schema):
            self.calls += 1
            return ModelResult(output=schema())

    model = EmptyLLM()
    monkeypatch.setattr(
        workflow,
        "build_pipeline",
        lambda cfg, run_id: build_pipeline(cfg, run_id, model, FakeEmbedder()),
    )
    result = CliRunner().invoke(workflow.app, ["doctor", "--live", "--config", str(config)])
    assert result.exit_code == 2, result.output
    assert "未形成完整的 299/300/301" in result.output
    assert "RAG 检查通过" not in result.output
    assert model.calls == 1


def test_known_workflow_error_explains_binding_failure(tmp_path, monkeypatch):
    from tc_rag.application.ports.errors import WorkflowError

    document = tmp_path / "fiction.pdf"
    document.touch()

    def mismatched(*args, **kwargs):
        raise WorkflowError("测试点与需求文档版本不一致")

    monkeypatch.setattr(workflow, "build_pipeline", mismatched)
    result = CliRunner().invoke(workflow.app, ["run", str(document), "-i", "生成测试点"])
    assert result.exit_code == 2
    assert "流程检查未通过：测试点与需求文档版本不一致" in result.output


def test_ctrl_c_during_execution_is_distinct_from_failure(tmp_path, monkeypatch):
    from test_pipeline import setup

    pipeline, document, config, _ = setup(tmp_path)
    monkeypatch.setattr(workflow, "build_pipeline", lambda cfg, run_id: pipeline)

    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow.asyncio, "run", interrupted)
    result = CliRunner().invoke(
        workflow.app,
        ["run", str(document), "--config", str(config), "-i", "生成测试点"],
    )
    assert result.exit_code == 130
    assert "已取消执行" in result.output
    assert "执行失败" not in result.output
