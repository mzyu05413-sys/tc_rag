import pytest
from pydantic import ValidationError

from tc_rag.core.config import load_config


def test_priority_and_deep_merge(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("retrieval:\n  mode: rag\n  vector_top_k: 7\n", encoding="utf-8")
    config = load_config(path, environ={"TC_RAG_RETRIEVAL__VECTOR_TOP_K": "9"},
                         cli_overrides={"retrieval": {"vector_top_k": 11}})
    assert config.retrieval.vector_top_k == 11
    assert config.retrieval.mode == "rag"
    assert config.retrieval.backend == "numpy"
    assert load_config(path, environ={
        "TC_RAG_RETRIEVAL__VECTOR_TOP_K": "9"}).retrieval.vector_top_k == 9


def test_missing_variable():
    with pytest.raises(ValueError, match="TC_RAG_LLM_BASE_URL"):
        load_config("config/example.yaml", environ={})


def test_example_and_no_secret_in_snapshot():
    env = {f"TC_RAG_{role}_{field}": value
           for role in ("LLM", "EMBEDDING")
           for field, value in (("BASE_URL", "https://example.test/v1"), ("MODEL", "demo"))}
    env["TC_RAG_LLM_API_KEY"] = "secret-sentinel-123"
    config = load_config("config/example.yaml", environ=env)
    assert config.models.llm.model == "demo"
    assert config.models.llm.api_key_env == "TC_RAG_LLM_API_KEY"
    assert env["TC_RAG_LLM_API_KEY"] not in repr(config)
    assert env["TC_RAG_LLM_API_KEY"] not in config.model_dump_json()


@pytest.mark.parametrize("override,field", [
    ({"retrieval": {"mode": "invalid"}}, "retrieval.mode"),
    ({"runtime": {"max_attempts": 0}}, "runtime.max_attempts"),
    ({"chunking": {"overlap_tokens": 900}}, "chunking"),
    ({"models": {"llm": {"api_key": "secret"}}}, "models.llm.api_key"),
])
def test_invalid_configuration(override, field):
    with pytest.raises(ValidationError) as error:
        load_config(environ={}, cli_overrides=override)
    assert field in str(error.value)


def test_defaults_file_matches_builtin():
    assert load_config("config/defaults.yaml", environ={}) == load_config(environ={})


def test_override_removes_unneeded_placeholder(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("project:\n  name: ${MISSING}\n", encoding="utf-8")
    assert load_config(path, environ={}, cli_overrides={
        "project": {"name": "override"}}).project.name == "override"


def test_invalid_root(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="root"):
        load_config(path, environ={})
