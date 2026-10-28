"""提供结果可复现的离线适配器，不自行推断业务输出。"""

import hashlib
import math
from collections import deque
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from tc_rag.application.ports.contracts import (
    Capabilities,
    EmbeddingBatch,
    EmbeddingSpace,
    ModelResult,
    T,
)


class FakeLLM:
    # Fake 模拟结构化接口，不根据需求猜测输出；所有业务内容由测试显式预设。
    capabilities = Capabilities(structured_output=True)

    def __init__(self, responses: list[dict[str, Any] | BaseModel | Exception] | None = None):
        self._responses = deque(responses or [])
        self.calls = 0

    async def generate(self, request: dict[str, Any], output_schema: type[T]) -> ModelResult:
        # 每次调用消费一项，允许成功、超时、非法输出等场景按顺序出现。
        self.calls += 1
        if not self._responses:
            raise RuntimeError("FakeLLM has no configured response")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        # 不把测试调用方提供的数据对象直接当成内部状态使用。
        payload = response.model_dump() if isinstance(response, BaseModel) else deepcopy(response)
        # 即使是 Fake，也要执行真实结构校验，避免测试绕过生产接口约束。
        output = output_schema.model_validate(payload)
        return ModelResult(output=output, request_id=f"fake-{self.calls}", finish_reason="stop")


class FakeEmbedder:
    # 哈希向量用于测试接口稳定性，不具有语义含义，不能用于评估召回质量。
    capabilities = Capabilities(batch=True)

    def __init__(self, responses: list[list[list[float]] | Exception] | None = None):
        self._responses = deque(responses or [])
        self.calls = 0

    async def embed_batch(self, texts: list[str], space: EmbeddingSpace) -> EmbeddingBatch:
        self.calls += 1
        if space.dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        if self._responses:
            response = self._responses.popleft()
            if isinstance(response, Exception):
                raise response
            vectors = deepcopy(response)
        else:
            vectors = []
            for text in texts:
                # 空间参数参与种子，避免不同模型配置误用同一组测试向量。
                seed = f"{space.provider}|{space.model}|{space.template_version}|{text}"
                digest = hashlib.shake_256(seed.encode()).digest(space.dimension * 4)
                vectors.append(
                    [
                        (int.from_bytes(digest[i : i + 4], "big") / (2**32 - 1)) * 2 - 1
                        for i in range(0, len(digest), 4)
                    ]
                )
        # 预设向量和默认生成向量都走同一组约束，保持条数与输入顺序对应。
        if len(vectors) != len(texts) or any(len(v) != space.dimension for v in vectors):
            raise ValueError("Embedding count or dimension mismatch")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("Embedding contains non-finite values")
        # L2 归一化把每条向量缩放为单位长度；零向量无法归一化，应明确失败。
        if space.normalize:
            normalized = []
            for vector in vectors:
                norm = math.sqrt(sum(value * value for value in vector))
                if norm == 0:
                    raise ValueError("Cannot normalize zero vector")
                normalized.append([value / norm for value in vector])
            vectors = normalized
        return EmbeddingBatch(vectors=vectors, space=space)
