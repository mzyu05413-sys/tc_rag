"""人工关键词的辅助评分，不以词面命中代替业务正确性。"""

import pytest
from pydantic import ValidationError

from tc_rag.application.services.vision_scoring import VisionGold, score_images


def row():
    return {"number": 1, "result": {"content": "不参与评分的展示文本"}, "structured": {
        "draft": "raw", "draft_valid": True, "model_verified": True,
        "context": {}, "views": [{"view_id": "overview"}],
        "description": {
            "image_type": "ui", "facts": [{
                "fact_id": "f1", "kind": "text", "text": "０／３００ 必 填",
                "view_ids": ["overview"],
            }],
            "tables": [{"headers": ["名称"], "rows": [["评论"]], "view_ids": ["overview"]}],
            "uncertainties": [{"text": "最多300字尚不能确定", "view_ids": ["overview"]}],
            "context_links": [{"text": "正文限定", "fact_ids": ["f1"], "context_ids": ["c1"]}],
        },
    }, "request_stats": {"requests": 2, "input_tokens": 6, "output_tokens": 4}}


def test_recall_normalization_tables_and_uncertainty_are_separate():
    gold = VisionGold.model_validate({"images": {"001": {
        "must_have": ["0/300", "必填", "评论", "正文限定"],
        "must_not_assert": ["最多300字"],
    }}})
    result = score_images([row()], gold)
    assert result["recall"] == .75 and result["unsupported_assertions"] == 0
    assert result["images"][0]["missing"] == ["正文限定"]
    assert result["images"][0]["request_stats"]["requests"] == 2
    assert "不能替代人工审核" in result["warning"]


def test_assertion_matches_once_per_keyword_and_unavailable_is_not_zero():
    image = row()
    image["structured"]["description"]["facts"][0]["text"] = "最多300字；最多300字"
    gold = VisionGold.model_validate({"images": {
        "001": {"must_have": ["必填"], "must_not_assert": ["最多300字"]},
        "003": {"must_have": ["缺失图"]}, "004": {"must_have": ["未执行图"]},
    }})
    result = score_images([image, {"number": 2}, {"number": 4}], gold)
    assert result["unsupported_assertions"] == 1 and result["recall"] == 0
    assert result["unlabeled_images"] == ["002"]
    assert result["unavailable_images"] == ["004"]
    assert result["gold_images_not_in_results"] == ["003"]
    assert result["images"][2]["recall"] is None


@pytest.mark.parametrize("targets", [[" "], ["０", "0"], ["必填", "必 填"]])
def test_invalid_or_duplicate_gold_targets_are_rejected(targets):
    with pytest.raises(ValidationError):
        VisionGold.model_validate({"images": {"001": {"must_have": targets}}})
