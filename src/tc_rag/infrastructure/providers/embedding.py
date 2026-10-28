"""OpenAI 兼容 embedding：响应按 index 对齐，不相信供应商返回顺序。"""

import asyncio
import re
from collections.abc import Callable
from time import perf_counter
from urllib.parse import urlsplit

import numpy as np
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from tc_rag.application.ports.contracts import Capabilities, EmbeddingBatch, EmbeddingSpace
from tc_rag.application.ports.errors import EmbeddingProviderError
from tc_rag.core.config import Endpoint, Runtime, resolve_api_key
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import ModelInvocation, Timing, Usage


class OpenAIEmbedder:
    capabilities = Capabilities(batch=True)

    def __init__(
        self,
        endpoint: Endpoint,
        runtime: Runtime,
        run_id: str,
        record: Callable[[ModelInvocation], None],
        client: AsyncOpenAI | None = None,
    ):
        if endpoint.adapter != "openai_sdk" or not endpoint.model or not endpoint.enabled:
            raise ValueError("embedding 需要启用 openai_sdk 并配置模型")
        if "@" in endpoint.base_url or "?" in endpoint.base_url:
            raise ValueError("embedding 端点不可包含凭据或查询参数")
        key = resolve_api_key(endpoint)
        if client is None and not key:
            raise ValueError("embedding 缺少 api_key 或有效的密钥环境变量")
        self.client = client or AsyncOpenAI(
            api_key=key,
            base_url=endpoint.base_url or None,
            max_retries=0,
            timeout=runtime.request_timeout_seconds,
        )
        self.endpoint, self.runtime, self.run_id, self.record = endpoint, runtime, run_id, record
        self.semaphore = asyncio.Semaphore(endpoint.concurrency)
        self.calls = 0
        self.admit: Callable[[], None] | None = None

    async def close(self) -> None:
        await self.client.close()

    def _status_code(self, exc: APIStatusError) -> str:
        # 只识别智谱官方端点的四位业务码；禁止回显第三方 message、请求或密钥。
        code = f"HTTP_{exc.status_code}"
        if urlsplit(self.endpoint.base_url).hostname != "open.bigmodel.cn":
            return code
        try:
            body = exc.response.json()
        except ValueError:
            return code
        detail = body.get("error") if isinstance(body, dict) else None
        business = str(detail.get("code", "")) if isinstance(detail, dict) else ""
        return code + "_BIGMODEL_" + business if re.fullmatch(r"[0-9]{4}", business) else code

    async def embed_batch(self, texts: list[str], space: EmbeddingSpace) -> EmbeddingBatch:
        if space.model != self.endpoint.model or space.dimension <= 0:
            raise ValueError("embedding 空间与端点不一致")
        batches = [
            texts[offset : offset + self.endpoint.batch_size]
            for offset in range(0, len(texts), self.endpoint.batch_size)
        ]
        results: list[tuple[list[list[float]], int | None] | None] = [None] * len(batches)
        pending = iter(enumerate(batches))

        async def worker() -> None:
            for index, batch in pending:
                results[index] = await self._embed_part(batch, space)

        # 单次大列表也按批并发；所有调用共享下方信号量，不突破端点总并发。
        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self.endpoint.concurrency, len(batches)))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            # 失败和取消时等待在途请求收尾，禁止后台继续扣预算或写调用记录。
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        vectors: list[list[float]] = []
        totals: list[int | None] = []
        for result in results:
            if result is None:
                raise RuntimeError("embedding 批次结果缺失")
            batch_vectors, total = result
            vectors.extend(batch_vectors)
            totals.append(total)
        return EmbeddingBatch(
            vectors,
            space,
            Usage(
                total_tokens=sum(t for t in totals if t is not None)
                if totals and all(t is not None for t in totals)
                else None
            ),
        )

    async def _embed_part(
        self, batch: list[str], space: EmbeddingSpace
    ) -> tuple[list[list[float]], int | None]:
        """执行一个 HTTP 批次；保留原有重试、响应校验和调用记账。"""
        async with self.semaphore:
            for attempt in range(self.runtime.max_attempts):
                if self.runtime.budget.max_cost is not None:
                    raise ValueError("未配置价格表，不能执行金额预算")
                if (
                    self.runtime.budget.max_calls is not None
                    and self.calls >= self.runtime.budget.max_calls
                ):
                    raise RuntimeError("达到 embedding 请求预算")
                self.calls += 1
                if self.admit:
                    self.admit()
                started, usage, error = perf_counter(), Usage(), None
                delay = min(2**attempt, 30)
                try:
                    response = await asyncio.wait_for(
                        self.client.embeddings.create(model=space.model, input=batch),
                        self.runtime.request_timeout_seconds,
                    )
                    usage = Usage(
                        input_tokens=response.usage.prompt_tokens,
                        output_tokens=0,  # embedding 不生成文本，输出 token 已知为零
                        total_tokens=response.usage.total_tokens,
                    )
                    indexes = [item.index for item in response.data]
                    if sorted(indexes) != list(range(len(batch))):
                        raise ValueError("embedding 响应 index 缺失或重复")
                    values = np.array(
                        [item.embedding for item in sorted(response.data, key=lambda x: x.index)],
                        dtype=np.float64,
                    )
                    if values.shape != (len(batch), space.dimension) or not np.all(
                        np.isfinite(values)
                    ):
                        raise ValueError("embedding 响应维度或数值非法")
                    if space.normalize:
                        scale = np.max(np.abs(values), axis=1, keepdims=True)
                        if np.any(scale == 0):
                            raise ValueError("不能归一化零向量")
                        values = values / scale
                        values /= np.linalg.norm(values, axis=1, keepdims=True)
                    return values.tolist(), usage.total_tokens
                except (APIConnectionError, APIStatusError, TimeoutError) as exc:
                    error = type(exc).__name__
                    if isinstance(exc, APIStatusError):
                        error = self._status_code(exc)
                        # 欠费无法靠秒级重试解决，立即停止，保留失败记录供恢复后排查。
                        if error == "HTTP_429_BIGMODEL_1113":
                            raise EmbeddingProviderError(error) from None
                        if exc.status_code not in {408, 409, 429} and exc.status_code < 500:
                            raise EmbeddingProviderError(error) from None
                        try:
                            delay = min(
                                60,
                                max(0, float(exc.response.headers.get("retry-after", delay))),
                            )
                        except ValueError:
                            pass
                    if attempt + 1 == self.runtime.max_attempts:
                        raise EmbeddingProviderError(error) from None
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    self.record(
                        ModelInvocation(
                            invocation_id=new_id("inv"),
                            run_id=self.run_id,
                            stage="embedding",
                            provider=space.provider,
                            model=space.model,
                            usage=usage,
                            timing=Timing(duration_ms=(perf_counter() - started) * 1000),
                            error=error,
                        )
                    )
                await asyncio.sleep(delay)
        raise RuntimeError("embedding 请求未完成")
