import pytest
from pydantic import ValidationError

from tc_rag.core.config import load_config, resolve_api_key


def test_priority_and_deep_merge(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("retrieval:\n  vector_top_k: 7\n", encoding="utf-8")
    config = load_config(
        path,
        environ={"TC_RAG_RETRIEVAL__VECTOR_TOP_K": "9"},
        cli_overrides={"retrieval": {"vector_top_k": 11}},
    )
    assert config.retrieval.vector_top_k == 11
    assert "mode" not in config.retrieval.model_dump()
    assert config.retrieval.backend == "numpy"
    assert (
        load_config(path, environ={"TC_RAG_RETRIEVAL__VECTOR_TOP_K": "9"}).retrieval.vector_top_k
        == 9
    )


def test_missing_variable():
    with pytest.raises(ValueError, match="TC_RAG_LLM_BASE_URL"):
        load_config("config/example.yaml", environ={})


def test_workdir_example_is_valid_and_can_configure_all_model_roles_offline():
    from tc_rag.bootstrap import validate_runtime_config

    template = load_config("workdir/local.example.yaml", environ={})
    for role in ("llm", "embedding", "vision", "rerank"):
        assert getattr(template.models, role).api_key is None
    assert template.models.llm.concurrency == 3
    assert template.models.embedding.batch_size == 16
    assert template.models.embedding.concurrency == 2
    assert template.models.vision.concurrency == 2
    assert "xlsx" in template.export.formats
    roles = {
        role: {
            "base_url": "https://fixture.invalid/rerank" if role == "rerank"
            else "https://fixture.invalid/v1",
            "model": "fixture-model",
            "api_key": "fake-offline-key",
            "enabled": True,
        }
        for role in ("llm", "embedding", "vision", "rerank")
    }
    roles["vision"]["supports_images"] = True
    config = load_config(
        "workdir/local.example.yaml", environ={},
        cli_overrides={"models": roles, "parsing": {"vision_enabled": True}},
    )
    validate_runtime_config(config)
    assert "fake-offline-key" not in config.model_dump_json()


def test_example_and_no_secret_in_snapshot():
    env = {
        f"TC_RAG_{role}_{field}": value
        for role in ("LLM", "EMBEDDING")
        for field, value in (("BASE_URL", "https://example.test/v1"), ("MODEL", "demo"))
    }
    env["TC_RAG_LLM_API_KEY"] = "secret-sentinel-123"
    config = load_config("config/example.yaml", environ=env)
    assert config.models.llm.model == "demo"
    assert config.models.llm.api_key_env == "TC_RAG_LLM_API_KEY"
    assert env["TC_RAG_LLM_API_KEY"] not in repr(config)
    assert env["TC_RAG_LLM_API_KEY"] not in config.model_dump_json()


@pytest.mark.parametrize(
    "override,field",
    [
        ({"retrieval": {"mode": "invalid"}}, "retrieval.mode"),
        ({"runtime": {"max_attempts": 0}}, "runtime.max_attempts"),
        ({"runtime": {"vision_min_interval_seconds": -1}}, "runtime.vision_min_interval_seconds"),
        (
            {"runtime": {"vision_min_interval_seconds": float("inf")}},
            "runtime.vision_min_interval_seconds",
        ),
        (
            {"runtime": {"vision_min_interval_seconds": float("nan")}},
            "runtime.vision_min_interval_seconds",
        ),
        ({"models": {"llm": {"max_output_tokens": 0}}}, "models.llm.max_output_tokens"),
        ({"models": {"llm": {"max_output_tokens": -1}}}, "models.llm.max_output_tokens"),
        ({"chunking": {"overlap_tokens": 900}}, "chunking"),
        ({"models": {"llm": {"api_key": {"invalid": "secret"}}}}, "models.llm.api_key"),
    ],
)
def test_invalid_configuration(override, field):
    with pytest.raises(ValidationError) as error:
        load_config(environ={}, cli_overrides=override)
    assert field in str(error.value)


def test_defaults_file_matches_builtin():
    assert load_config("config/defaults.yaml", environ={}) == load_config(environ={})


def test_override_removes_unneeded_placeholder(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("project:\n  name: ${MISSING}\n", encoding="utf-8")
    assert (
        load_config(path, environ={}, cli_overrides={"project": {"name": "override"}}).project.name
        == "override"
    )


def test_invalid_root(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="root"):
        load_config(path, environ={})


@pytest.mark.parametrize("response_format", ["json_schema", "json_object"])
def test_generation_response_format_is_explicit(response_format):
    config = load_config(
        environ={}, cli_overrides={"models": {"llm": {"response_format": response_format}}}
    )
    assert config.models.llm.response_format == response_format
    assert config.model_dump()["models"]["llm"]["response_format"] == response_format
    with pytest.raises(ValidationError):
        load_config(environ={}, cli_overrides={"models": {"llm": {"response_format": "text"}}})


@pytest.mark.parametrize("role", ["llm", "embedding", "vision", "rerank"])
def test_yaml_secret_is_usable_but_excluded_from_snapshots(tmp_path, role):
    secret = "fixture-file-secret-do-not-export"
    path = tmp_path / "local.yaml"
    path.write_text(f'models:\n  {role}:\n    api_key: " {secret} "\n', encoding="utf-8")
    config = load_config(path, environ={})
    endpoint = getattr(config.models, role)
    assert resolve_api_key(endpoint, environ={}) == secret
    assert secret not in repr(config)
    assert secret not in config.model_dump_json()
    assert "api_key" not in config.model_dump()["models"][role]
    assert "api_key" not in config.model_dump(mode="json")["models"][role]
    # 即便调用方显式 include，也不能把密钥加入快照。
    assert endpoint.model_dump(include={"api_key"}) == {}
    assert resolve_api_key(endpoint.model_copy(deep=True), environ={}) == secret


@pytest.mark.parametrize("configured", [None, "", "  ", "local-file-key"])
def test_nonempty_file_key_wins_and_empty_key_falls_back(configured):
    config = load_config(
        environ={},
        cli_overrides={"models": {"llm": {"api_key": configured}}},
    )
    endpoint = config.models.llm
    assert resolve_api_key(endpoint, environ={"TC_RAG_LLM_API_KEY": " legacy-env-key "}) == (
        "local-file-key" if configured == "local-file-key" else "legacy-env-key"
    )
    assert resolve_api_key(endpoint, environ={}) == (
        "local-file-key" if configured == "local-file-key" else ""
    )


def test_nested_environment_and_cli_can_override_file_key(tmp_path):
    path = tmp_path / "local.yaml"
    path.write_text('models:\n  llm:\n    api_key: "file-key"\n', encoding="utf-8")
    env = {"TC_RAG_MODELS__LLM__API_KEY": "nested-env-key"}
    endpoint = load_config(path, environ=env).models.llm
    assert resolve_api_key(endpoint, environ={}) == "nested-env-key"
    config = load_config(
        path, environ=env, cli_overrides={"models": {"llm": {"api_key": "cli-key"}}}
    )
    assert resolve_api_key(config.models.llm, environ={}) == "cli-key"


def test_config_error_does_not_print_another_fields_secret():
    secret = "fixture-config-validation-secret"
    with pytest.raises(ValidationError) as exc:
        load_config(
            environ={},
            cli_overrides={"models": {"llm": {"api_key": secret, "concurrency": 0}}},
        )
    assert secret not in str(exc.value)
