"""实验用词元过滤，不改变生产默认分词。"""

import unicodedata


def keep_token(token: str) -> bool:
    """去除空白、纯标点；保留符号、字母、数字及中文。"""
    return bool(token.strip()) and not all(
        ch.isspace() or unicodedata.category(ch).startswith("P") for ch in token
    )
