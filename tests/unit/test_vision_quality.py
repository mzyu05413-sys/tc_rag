"""图片像素覆盖、来源引用和上下文边界，不使用真实模型。"""

import io
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from tc_rag.application.ports.errors import VisionReadError
from tc_rag.application.services.vision_content import image_context, render_description
from tc_rag.core.config import VisionProcessing
from tc_rag.domain.models import DocumentBlock, Locator
from tc_rag.domain.vision import VisionDescription
from tc_rag.infrastructure.providers.vision_images import prepare_views


def picture(size, *, mode="RGB", color="white"):
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_long_picture_tiles_cover_all_original_pixels_and_keep_bottom_marker():
    original = Image.new("RGB", (600, 4200), "white")
    original.putpixel((599, 4199), (255, 0, 0))
    buffer = io.BytesIO()
    original.save(buffer, format="PNG")
    views = prepare_views(buffer.getvalue(), VisionProcessing(max_edge=1024, tile_edge=1024))
    assert len(views) > 2 and views[0].view_id == "overview"
    assert max(views[0].size) <= 1024
    covered = set()
    for view in views[1:]:
        x0, y0, x1, y1 = view.bounds
        assert (x0, x1) == (0, 600)
        assert view.size == (x1 - x0, y1 - y0)
        covered.update(range(y0, y1))
        with Image.open(io.BytesIO(view.png)) as crop:
            assert crop.tobytes() == original.crop(view.bounds).tobytes()
    assert covered == set(range(4200))
    with Image.open(io.BytesIO(views[-1].png)) as last:
        assert last.getpixel((last.width - 1, last.height - 1)) == (255, 0, 0)


def test_wide_picture_has_no_gaps_between_horizontal_tiles():
    views = prepare_views(picture((4200, 600)), VisionProcessing(max_edge=1024, tile_edge=1024))
    covered = {x for view in views[1:] for x in range(view.bounds[0], view.bounds[2])}
    assert covered == set(range(4200))
    assert all(left.bounds[2] > right.bounds[0] for left, right in zip(views[1:], views[2:]))


def test_transparent_text_background_becomes_white_without_enlargement():
    views = prepare_views(
        picture((20, 10), mode="RGBA", color=(0, 0, 0, 0)), VisionProcessing(min_edge=0)
    )
    assert len(views) == 1 and views[0].size == (20, 10)
    with Image.open(io.BytesIO(views[0].png)) as result:
        assert result.getpixel((0, 0)) == (255, 255, 255)


def test_too_many_tiles_is_explicit_failure_not_truncated_coverage():
    with pytest.raises(VisionReadError, match="IMAGE_VIEW_BUDGET"):
        prepare_views(picture((600, 4200)), VisionProcessing(max_edge=1024, max_tiles=1))


@pytest.mark.parametrize(
    "kwargs", [{"tile_overlap": 800}, {"max_tiles": 65}, {"context_chars": -1}]
)
def test_bad_visual_budgets_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        VisionProcessing(**kwargs)


def block(key, text, *, kind="paragraph", chapter="A", origin="text"):
    return DocumentBlock(
        block_id=key, document_id="d", kind=kind, content=text, order=0,
        locator=Locator(), heading_path=[chapter], origin=origin,
    )


def test_context_includes_both_sides_but_not_other_chapter_or_vision_text():
    blocks = [
        block("outside", "外部规则", chapter="B"), block("before", "必须填写说明"),
        block("image", "image asset", kind="image"),
        block("old", "以前的图片解释", origin="vision"),
        block("after", "不得仅上传附件"), block("next", "其他功能", chapter="B"),
    ]
    result = json.loads(image_context(blocks, 2, 2, 6000))
    assert {item["block_id"] for item in result["neighbors"]} == {"before", "after"}
    assert {item["position"] for item in result["neighbors"]} == {"before", "after"}
    assert "不得" in result["neighbors"][1]["text"]


def test_context_budget_keeps_slices_auditable_and_includes_following_text():
    blocks = [block("a", "A" * 100), block("i", "image", kind="image"), block("b", "B" * 100)]
    result = json.loads(image_context(blocks, 1, 2, 20))
    assert len(result["neighbors"]) == 2
    for item in result["neighbors"]:
        assert len(item["text"]) == item["end_char"] - item["start_char"] == 10
        assert item["truncated"]


def test_context_keeps_original_reading_order_before_image():
    blocks = [block("a", "首先"), block("b", "其次"), block("i", "image", kind="image")]
    context = json.loads(image_context(blocks, 2, 2, 6000))
    assert [item["block_id"] for item in context["neighbors"]] == ["a", "b"]


def description():
    return {
        "image_type": "ui", "facts": [
            {"fact_id": "f1", "kind": "text", "text": "0/300", "view_ids": ["overview"]}
        ], "context_links": [{"text": "正文解释了单位", "fact_ids": ["f1"], "context_ids": ["c1"]}],
    }


@pytest.mark.parametrize("change", ["view", "fact", "context", "duplicate", "empty"])
def test_visual_contract_rejects_unknown_references_and_empty_outputs(change):
    payload = description()
    if change == "view":
        payload["facts"][0]["view_ids"] = ["invented"]
    elif change == "fact":
        payload["context_links"][0]["fact_ids"] = ["invented"]
    elif change == "context":
        payload["context_links"][0]["context_ids"] = ["invented"]
    elif change == "duplicate":
        payload["facts"].append(payload["facts"][0])
    else:
        payload = {"image_type": "other"}
    with pytest.raises(ValueError):
        VisionDescription.model_validate(payload).check_references({"overview"}, {"c1"})


def test_render_preserves_visible_fact_and_separates_context_explanation():
    value = VisionDescription.model_validate(description())
    value.check_references({"overview"}, {"c1"})
    text = render_description(value)
    assert "0/300" in text and "300字" not in text
    assert "图片可见事实" in text and "正文关联解释" in text and "仍待核实" in text


def test_unclear_picture_can_report_uncertainty_instead_of_fabricating_fact():
    value = VisionDescription.model_validate({
        "image_type": "other",
        "uncertainties": [{"text": "图中文字无法辨认", "view_ids": ["overview"]}],
    })
    value.check_references({"overview"}, set())
    assert "无法辨认" in render_description(value) and not value.facts
