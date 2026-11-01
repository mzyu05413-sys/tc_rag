"""标题展示规范化；只改变导出文本，不改领域事实。"""

import re

_BANNED_PREFIX = re.compile(r"^(?:验证|确认|待确认|需确认)[:：\- \t]*")
_BANNED_PHRASES = ("与需求确认", "待确认", "需确认", "未在图中出现")


def display_title(text: str) -> str:
    """去掉不确定/审核语气和常见黑名单词；空结果保留原文。"""
    value = _BANNED_PREFIX.sub("", text.strip(" "))
    for phrase in _BANNED_PHRASES:
        value = value.replace(phrase, "")
    value = re.sub(r" {2,}", " ", value).strip(" ")
    return value or text.strip(" ")
