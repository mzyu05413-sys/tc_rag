"""新能力显式选择，旧默认快照及配置错误的敏感信息边界保持不变。"""

import json

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from tc_rag.core.config import AppConfig, load_config
from tc_rag.domain.pipeline import CaseBatch
from tc_rag.interfaces.cli import app
from tc_rag.interfaces.workflow import _error


def test_optional_features_keep_legacy_default_snapshot_and_active_fingerprint():
    default = AppConfig()
    assert default.parsing.infer_docx_headings is False
    assert default.export.point_tree_layout == "module"
    snapshot = default.model_dump(mode="json")
    assert "infer_docx_headings" not in snapshot["parsing"]
    assert snapshot["export"]["point_tree_layout"] == "module"
    assert AppConfig.model_validate(snapshot).model_dump(mode="json") == snapshot
    active = load_config(environ={}, cli_overrides={
        "parsing": {"infer_docx_headings": True}, "export": {"point_tree_layout": "chapter"},
    })
    active_snapshot = active.model_dump(mode="json")
    assert active_snapshot["parsing"]["infer_docx_headings"] is True
    assert active_snapshot["export"]["point_tree_layout"] == "chapter"
    assert AppConfig.model_validate(active_snapshot).model_dump(mode="json") == active_snapshot
    assert active_snapshot != snapshot


@pytest.mark.parametrize("setting", [
    {"parsing": {"infer_docx_headings": "sometimes"}},
    {"export": {"point_tree_layout": "unknown"}},
    {"planning": {"semantic_dedupe_enabled": True}},
])
def test_invalid_new_settings_and_unadopted_semantic_route_are_rejected(setting):
    with pytest.raises(ValidationError):
        load_config(environ={}, cli_overrides=setting)


@pytest.mark.parametrize("setting,field", [
    ({"generation": {"batch_point_limit": 0}}, "generation.batch_point_limit"),
    ({"models": {"llm": {"private-unknown-key": "private-value"}}}, "models.llm"),
    ({"private-root-key": {"api_key": "private-value"}}, "<未知字段>"),
])
def test_configuration_errors_show_known_fields_without_values_or_unknown_keys(
    tmp_path, monkeypatch, setting, field
):
    monkeypatch.setattr("tc_rag.interfaces.workflow.validate_runtime_config", lambda config: None)
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(setting), encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--config", str(path)])
    assert result.exit_code == 2, result.output
    assert "配置校验失败" in result.output and field in result.output
    assert "private-" not in result.output


def test_domain_validation_is_not_misreported_as_configuration(capsys):
    with pytest.raises(ValidationError) as invalid:
        CaseBatch.model_validate({"cases": "private-value"})
    with pytest.raises(typer.Exit):
        _error(invalid.value)
    message = capsys.readouterr().err
    assert "数据契约校验失败" in message
    assert "配置校验失败" not in message and "private-" not in message


def test_estimate_respects_optional_docx_heading_setting(tmp_path, monkeypatch):
    from docx import Document

    from tc_rag.infrastructure.parsing import docx as parsing

    original = parsing.DocxParser
    received = []

    def parser(store, *, infer_headings=False):
        received.append(infer_headings)
        return original(store, infer_headings=infer_headings)

    monkeypatch.setattr(parsing, "DocxParser", parser)
    document = tmp_path / "fixture.docx"
    doc = Document()
    doc.add_paragraph("数量最多300个。")
    doc.save(document)
    config = tmp_path / "fixture.json"
    config.write_text(json.dumps({
        "parsing": {"infer_docx_headings": True},
        "chunking": {"token_counter": "character_approx"},
    }), encoding="utf-8")
    result = CliRunner().invoke(app, ["estimate", str(document), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert received == [True]
