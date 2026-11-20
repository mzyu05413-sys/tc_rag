import runpy

import pytest

profile = runpy.run_path("eval/kb/audit_feature_rerank_inputs.py")["profile"]


def candidate(block, start, end):
    return dict(text="input", sources=[dict(block_id=block, start_char=start, end_char=end)])


def test_mid_section_slice_remains_context_restatement():
    text = ("【图片可见事实】\n有按钮。\n【正文关联解释】\n正文复述权限。\n"
            "【无法辨认、歧义与缺漏】\n未见点击结果。")
    start = text.index("正文复述")
    end = start + len("正文复述权限。")
    result = profile(candidate("v", start, end), {
        "v": dict(origin="vision", content=text)})
    assert result["source_char_counts"]["vision_context_restatement"] == end-start
    assert result["source_char_counts"]["vision_visible_facts"] == 0
    assert result["source_char_total"] == end-start


def test_body_and_visible_fact_characters_kept_separate():
    text = "【图片可见事实】\n有按钮。"
    row = candidate("v", 0, len(text))
    row["sources"].append(dict(block_id="b", start_char=0, end_char=2))
    result = profile(row, dict(v=dict(origin="vision", content=text),
                               b=dict(origin="text", content="正文")))
    assert result["source_char_counts"]["body"] == 2
    assert result["source_char_counts"]["vision_visible_facts"] == len(text)
    assert result["source_char_total"] == len(text)+2


def test_missing_headers_not_assumed_visible_fact():
    result = profile(candidate("v", 0, 3), dict(v=dict(origin="vision", content="未分类")))
    assert result["source_char_counts"] == {"vision_unclassified": 3}


def test_invalid_source_interval_rejected():
    with pytest.raises(ValueError, match="来源区间"):
        profile(candidate("v", 0, 20), dict(v=dict(origin="vision", content="short")))
