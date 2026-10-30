import pytest
from typer.testing import CliRunner

from tc_rag.interfaces.cli import app

runner = CliRunner()


@pytest.mark.parametrize("command", ["run", "start"])
@pytest.mark.parametrize("flag", ["--with-code", "--no-code"])
def test_removed_code_flags_are_not_accepted(tmp_path, command, flag):
    document = tmp_path / "fixture.docx"
    document.touch()
    arguments = [command] + ([str(document)] if command == "run" else [])
    result = runner.invoke(app, [*arguments, flag, "--preview"])
    assert result.exit_code == 2
    assert "No such option" in result.output


@pytest.mark.parametrize("origin", ["yaml", "environment"])
def test_obsolete_sources_configuration_has_safe_migration_message(tmp_path, monkeypatch, origin):
    document, config = tmp_path / "fixture.docx", tmp_path / "old.yaml"
    document.touch()
    private = "private-legacy-reference"
    if origin == "yaml":
        config.write_text(f"sources:\n  code:\n    repo: {private}\n", encoding="utf-8")
    else:
        config.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("TC_RAG_SOURCES__CODE__REPO", private)
    result = runner.invoke(app, ["run", str(document), "--config", str(config), "--preview"])
    assert result.exit_code == 2
    assert "代码读取分支已移除" in result.output
    assert "TC_RAG_SOURCES__*" in result.output
    assert private not in result.output


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("TC_RAG_"):
            monkeypatch.delenv(key)


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ["run", "status", "inspect", "export", "estimate"]:
        assert command in result.output


def test_no_args_prints_help_when_not_interactive():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "run" in result.output and "start" in result.output


def test_no_args_starts_interactive_when_tty(monkeypatch):
    from tc_rag.interfaces import workflow

    class TTY:
        def isatty(self) -> bool:
            return True

    called = {"started": False}

    def fake_start(**kwargs) -> None:
        called["started"] = True

    class Ctx:
        invoked_subcommand = None

    monkeypatch.setattr(workflow.sys, "stdin", TTY())
    monkeypatch.setattr(workflow.sys, "stdout", TTY())
    monkeypatch.setattr(workflow, "_interactive_start", fake_start)
    workflow.main(Ctx())  # type: ignore[arg-type]
    assert called["started"] is True


def test_rag_preview_is_continuous_and_has_no_artifacts(tmp_path):
    doc = tmp_path / "demo.docx"
    doc.write_bytes(b"placeholder")
    result = runner.invoke(app, ["run", str(doc)])
    assert result.exit_code == 0, result.output
    assert "RAG 唯一流程" in result.output
    stages = result.output.splitlines()[-1].split(" → ")
    assert stages.index("index") < stages.index("plan_points")
    assert stages.index("plan_points") < stages.index("generate") < stages.index("export_cases")
    assert "review" not in stages and "awaiting_review" not in stages
    assert stages.count("retrieve") == 2
    assert list(tmp_path.iterdir()) == [doc]


@pytest.mark.parametrize("legacy_mode", ["direct", "rag"])
def test_legacy_mode_configuration_explains_migration(tmp_path, legacy_mode):
    doc, config = tmp_path / "demo.pdf", tmp_path / "config.yaml"
    doc.touch()
    config.write_text(f"retrieval:\n  mode: {legacy_mode}\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(doc), "--config", str(config)])
    assert result.exit_code == 2
    assert "请删除 retrieval.mode" in result.output


@pytest.mark.parametrize("command", ["run", "start"])
def test_help_has_no_route_switch(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "--mode" not in result.output
    assert "direct" not in result.output


@pytest.mark.parametrize("command", ["run", "start"])
def test_retrieval_switches_are_visible(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "--metric" in result.output
    assert "--normalize" in result.output and "--no-normalize" in result.output


def test_removed_comparison_is_not_executable():
    result = runner.invoke(app, ["compare"])
    assert result.exit_code == 2


def test_removed_review_command_is_not_executable():
    assert runner.invoke(app, ["review"]).exit_code == 2


def test_legacy_mode_environment_is_explained(tmp_path, monkeypatch):
    document = tmp_path / "fixture.pdf"
    document.touch()
    monkeypatch.setenv("TC_RAG_RETRIEVAL__MODE", "rag")
    result = runner.invoke(app, ["run", str(document)])
    assert result.exit_code == 2
    assert "请删除 retrieval.mode" in result.output
    assert "TC_RAG_RETRIEVAL__MODE" in result.output


def test_old_planning_switch_cannot_disable_rag(tmp_path):
    document, config = tmp_path / "fixture.pdf", tmp_path / "old.yaml"
    document.touch()
    config.write_text("planning:\n  llm_supplement: false\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(document), "--config", str(config)])
    assert result.exit_code == 2
    assert "RAG 规划已固定启用" in result.output


def test_bad_paths_modes_and_config(tmp_path):
    doc = tmp_path / "demo.txt"
    doc.touch()
    assert runner.invoke(app, ["run", str(doc)]).exit_code == 2
    assert runner.invoke(app, ["run", str(tmp_path / "missing.pdf")]).exit_code == 2
    assert runner.invoke(app, ["run", str(doc), "--mode", "invalid"]).exit_code == 2
    doc = tmp_path / "demo.pdf"
    doc.touch()
    result = runner.invoke(app, ["run", str(doc), "--config", "config/example.yaml"])
    assert result.exit_code == 2
    assert "ingest" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["status", "demo"],
        ["inspect", "demo", "--stage", "rules"],
        ["export", "demo", "--format", "json"],
    ],
)
def test_missing_run_returns_error(args, tmp_path, monkeypatch):
    monkeypatch.setenv("TC_RAG_STORAGE__DATABASE", str(tmp_path / "data.sqlite"))
    result = runner.invoke(app, args)
    assert result.exit_code == 2
