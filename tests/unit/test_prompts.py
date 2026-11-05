"""主线提示词固定装载已发布版本，不因其他资源出现而隐式切换。"""

import re
from pathlib import Path
from typing import Any, get_args

import pytest

from tc_rag.bootstrap import PROMPT_VERSIONS, load_prompts
from tc_rag.domain.models import BusinessEntity, Issue, RequirementRule, TestPoint

STAGES = (
    "extract", "plan_points", "generate", "rag_context", "vision", "vision_verify", "case_skill"
)
REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def _literals(model: Any, field: str) -> set[str]:
    annotation = model.model_fields[field].annotation
    return {value for value in get_args(annotation) if isinstance(value, str)}


def _mentions(text: str, value: str) -> bool:
    """按独立词匹配，避免 state 被 state_transition 误判为已列出。"""
    return re.search(rf"(?<![a-z_]){re.escape(value)}(?![a-z_])", text) is not None


def fake_root(tmp_path: Path) -> Path:
    for stage in STAGES:
        (tmp_path / stage).mkdir()
        for version in ("v1", "v2"):
            (tmp_path / stage / f"{version}.md").write_text(
                f"{stage} {version}", encoding="utf-8"
            )
    return tmp_path


def test_document_rag_uses_only_published_prompt_versions(tmp_path):
    prompts, versions = load_prompts(fake_root(tmp_path))
    assert versions == PROMPT_VERSIONS
    assert prompts == {
        stage: f"{stage} {PROMPT_VERSIONS[stage]}" for stage in STAGES
    }


def test_missing_published_prompt_does_not_fall_back_to_experimental_version(tmp_path):
    root = fake_root(tmp_path)
    (root / "generate" / f"{PROMPT_VERSIONS['generate']}.md").unlink()
    with pytest.raises(FileNotFoundError):
        load_prompts(root)


def test_extraction_context_prompt_is_explicit_versioned_and_packaged(tmp_path):
    import tomllib

    root = fake_root(tmp_path)
    assert "extract_context" not in load_prompts(root)[0]
    with pytest.raises(FileNotFoundError):
        load_prompts(root, extraction_context=True)
    (root / "extract_context").mkdir()
    (root / "extract_context/v2.md").write_text("背景不能扩大引用范围", encoding="utf-8")
    prompts, versions = load_prompts(root, extraction_context=True)
    assert prompts["extract_context"] == "背景不能扩大引用范围"
    assert versions == {**PROMPT_VERSIONS, "extract_context": "v2"}
    repo = REPO_PROMPTS.parent
    package = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    files = package["tool"]["setuptools"]["data-files"]["share/tc_rag/prompts/extract_context"]
    assert files == ["prompts/extract_context/v1.md", "prompts/extract_context/v2.md"]
    assert all((repo / item).is_file() for item in files)


def test_extraction_context_v2_enforces_ranges_even_with_same_block_id():
    prompts, versions = load_prompts(REPO_PROMPTS, extraction_context=True)
    assert versions["extract_context"] == "v2"
    text = prompts["extract_context"]
    assert "不能只凭 block_id 相同扩大引用范围" in text
    assert "不能拼接不连续区间" in text
    assert "source_window_v2" in text and "issues" in text


CLOSED_ENUMS: dict[str, list[tuple[Any, str]]] = {
    "extract": [
        (RequirementRule, "kind"),
        (RequirementRule, "status"),
        (BusinessEntity, "kind"),
        (Issue, "kind"),
        (Issue, "severity"),
    ],
    "plan_points": [
        (TestPoint, "technique"),
        (TestPoint, "applicability"),
        (TestPoint, "status"),
        (Issue, "kind"),
        (Issue, "severity"),
    ],
}


@pytest.mark.parametrize("stage", sorted(CLOSED_ENUMS))
def test_published_prompt_lists_every_closed_enum_value(stage):
    prompts, _ = load_prompts(REPO_PROMPTS)
    text = prompts[stage]
    for model, field in CLOSED_ENUMS[stage]:
        for value in _literals(model, field):
            assert _mentions(text, value), (
                f"{stage} 提示词未列出 {model.__name__}.{field} 的枚举值 {value}"
            )


@pytest.mark.parametrize("stage", ("extract", "plan_points"))
def test_published_prompt_forbids_fields_outside_schema(stage):
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "Schema 之外的" in prompts[stage]


def test_extract_prompt_requires_object_attributes():
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "entities[].attributes 必须是 JSON 对象" in prompts["extract"]


def test_extract_prompt_defines_rule_kind_rubric():
    prompts, _ = load_prompts(REPO_PROMPTS)
    text = prompts["extract"]
    assert "规则类型按以下顺序判定" in text
    for kind in (
        "field_constraint",
        "error_behavior",
        "permission",
        "state_transition",
        "workflow",
        "business_condition",
    ):
        assert kind in text


def test_plan_points_prompt_keeps_issues_out_of_points():
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "points 对象内不得出现 issues 字段" in prompts["plan_points"]


def test_extract_prompt_classifies_display_as_other():
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "kind=other" in prompts["extract"]
    assert "不得据此编造交互" in prompts["extract"]


def test_plan_points_prompt_merges_ui_display():
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "同一功能最多补充一条概括展示点" in prompts["plan_points"]
    assert "界面展示：" in prompts["plan_points"]


def test_generate_prompt_summarizes_ui_display():
    prompts, _ = load_prompts(REPO_PROMPTS)
    assert "界面展示汇总" in prompts["generate"]
    assert "只生成一条概括用例" in prompts["generate"]


@pytest.mark.parametrize("stage", ("extract", "plan_points", "generate"))
def test_v2_keeps_current_v1_in_order(stage):
    def nonblank(path: Path) -> list[str]:
        return [
            line.rstrip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    v1 = nonblank(REPO_PROMPTS / stage / "v1.md")
    v2 = nonblank(REPO_PROMPTS / stage / "v2.md")
    candidates = iter(v2)
    assert all(any(candidate == line for candidate in candidates) for line in v1)
