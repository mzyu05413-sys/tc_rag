"""关键词召回与 RRF；仅融合排名，不混加向量相似度和词频分数。"""

import importlib
import logging
from collections import Counter
from collections.abc import Callable


class KeywordIndex:
    def __init__(self, tokenizer: Callable[[str], list[str]] | None = None):
        if tokenizer is None:
            try:
                jieba = importlib.import_module("jieba")
            except ImportError:
                raise ValueError('关键词召回需要安装 pip install -e ".[keyword]"') from None
            except Exception as exc:
                raise ValueError(
                    "jieba 加载失败，请检查环境兼容性：" + type(exc).__name__
                ) from None
            jieba.setLogLevel(logging.WARNING)

            def segment(text: str) -> list[str]:
                return list(jieba.cut(text))

            tokenizer = segment
        self.tokenizer = tokenizer
        self.documents: dict[str, Counter[str]] = {}

    def build(self, documents: dict[str, str]) -> None:
        self.documents = {
            key: Counter(t for t in self.tokenizer(text.lower()) if t.strip())
            for key, text in documents.items()
        }

    def search(self, query: str, top_k: int, allowed: set[str]) -> list[str]:
        tokens = set(self.tokenizer(query.lower()))
        scores = [
            (key, sum(counts[t] for t in tokens))
            for key, counts in self.documents.items()
            if key in allowed
        ]
        return [
            key
            for key, score in sorted(scores, key=lambda item: (-item[1], item[0]))[:top_k]
            if score > 0
        ]


def rrf(rankings: list[list[str]], limit: int, constant: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        if len(ranking) != len(set(ranking)):
            raise ValueError("同一召回排名存在重复 ID")
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (constant + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
