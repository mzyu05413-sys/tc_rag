"""导出标题只去审核语气和黑名单词，不改领域事实。"""

from tc_rag.domain.text import display_title


def test_display_title_strips_review_language():
    assert display_title("验证学习承诺为必填。") == "学习承诺为必填。"
    assert display_title("确认：学习计划待确认") == "学习计划"
    assert display_title("确认：学习计划与需求确认") == "学习计划"


def test_display_title_keeps_non_empty_content():
    assert display_title("待确认") == "待确认"
    assert display_title("学习承诺展示") == "学习承诺展示"
