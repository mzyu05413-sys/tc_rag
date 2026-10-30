"""主线提示词固定装载已发布版本，不因其他资源出现而隐式切换。"""

from pathlib import Path

import pytest

from tc_rag.bootstrap import load_prompts

STAGES = ("extract", "plan_points", "generate", "rag_context", "vision", "case_skill")


def fake_root(tmp_path: Path) -> Path:
    for stage in STAGES:
        (tmp_path / stage).mkdir()
        (tmp_path / stage / "v1.md").write_text(f"{stage} base", encoding="utf-8")
        (tmp_path / stage / "v2.md").write_text("unused experimental text", encoding="utf-8")
    return tmp_path


def test_document_rag_uses_only_published_prompt_versions(tmp_path):
    prompts, versions = load_prompts(fake_root(tmp_path))
    assert set(versions.values()) == {"v1"}
    assert prompts == {stage: f"{stage} base" for stage in STAGES}


def test_missing_published_prompt_does_not_fall_back_to_experimental_version(tmp_path):
    root = fake_root(tmp_path)
    (root / "generate" / "v1.md").unlink()
    with pytest.raises(FileNotFoundError):
        load_prompts(root)
