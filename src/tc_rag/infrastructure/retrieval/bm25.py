"""内存BM25倒排索引；统计范围是build传入语料，过滤不重新计算IDF。"""

import math
from collections.abc import Callable

from tc_rag.infrastructure.retrieval.keyword import KeywordIndex


class BM25Index(KeywordIndex):
    def __init__(
        self, tokenizer: Callable[[str], list[str]] | None = None, *,
        k1: float = 1.2, b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0 or not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("BM25参数非法")
        super().__init__(tokenizer)
        self.k1, self.b = k1, b
        self.postings: dict[str, dict[str, int]] = {}
        self.lengths: dict[str, int] = {}
        self.average_length = 0.0

    def build(self, documents: dict[str, str]) -> None:
        super().build(documents)
        self.postings = {}
        self.lengths = {key: sum(counts.values()) for key, counts in self.documents.items()}
        self.average_length = sum(self.lengths.values()) / len(documents) if documents else 0
        for key, counts in self.documents.items():
            for token, count in counts.items():
                self.postings.setdefault(token, {})[key] = count

    def search(self, query: str, top_k: int, allowed: set[str]) -> list[str]:
        if top_k <= 0:
            raise ValueError("top_k必须为正")
        if not self.average_length:
            return []
        scores: dict[str, float] = {}
        n = len(self.documents)
        for token in sorted(set(self.tokenizer(query.lower()))):
            posting = self.postings.get(token, {})
            df = len(posting)
            idf = math.log1p((n - df + 0.5) / (df + 0.5))
            for key, tf in posting.items():
                if key not in allowed:
                    continue
                norm = self.k1 * (1 - self.b + self.b * self.lengths[key]/self.average_length)
                scores[key] = scores.get(key, 0) + idf * tf * (self.k1 + 1)/(tf + norm)
        return [key for key, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_k]]
