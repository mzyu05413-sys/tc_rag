"""智谱 REST 重排协议；异常交给检索服务记录并回退原排名。"""

import math
from collections.abc import Callable
from time import perf_counter

import httpx

from tc_rag.application.ports.contracts import RankedCandidate
from tc_rag.core.config import Endpoint, Runtime, resolve_api_key
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import ModelInvocation, Timing, Usage


class HttpReranker:
    def __init__(
        self,
        endpoint: Endpoint,
        runtime: Runtime,
        run_id: str,
        record: Callable[[ModelInvocation], None],
        client: httpx.AsyncClient | None = None,
    ):
        if endpoint.adapter != "http" or not endpoint.enabled or not endpoint.model:
            raise ValueError("rerank 需要启用 http 适配器并配置模型")
        if not endpoint.base_url or "@" in endpoint.base_url or "?" in endpoint.base_url:
            raise ValueError("rerank.base_url 必须是无凭据的完整 /rerank 地址")
        self.key = resolve_api_key(endpoint)
        if client is None and not self.key:
            raise ValueError("rerank 缺少 api_key 或有效的密钥环境变量")
        self.client = client or httpx.AsyncClient(timeout=runtime.request_timeout_seconds)
        self.endpoint, self.runtime, self.run_id, self.record = endpoint, runtime, run_id, record
        self.admit: Callable[[], None] | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def rank(self, query: str, candidates: list[RankedCandidate]) -> list[RankedCandidate]:
        if not candidates:
            return []
        if (
            len(candidates) > 128
            or len(query) > 4096
            or any(len(c.text) > 4096 for c in candidates)
        ):
            raise ValueError("候选数量或文本长度超过智谱 rerank 协议限制")
        if self.runtime.budget.max_cost is not None:
            raise ValueError("未配置价格表，不能执行金额预算")
        if self.admit:
            self.admit()
        started, usage, error = perf_counter(), Usage(), None
        try:
            response = await self.client.post(
                self.endpoint.base_url,
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "model": self.endpoint.model,
                    "query": query,
                    "documents": [c.text for c in candidates],
                    "top_n": len(candidates),
                },
                timeout=self.runtime.request_timeout_seconds,
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP_{response.status_code}")
            data = response.json()
            rows = data["results"]
            indexes = [r["index"] for r in rows]
            if any(type(i) is not int for i in indexes) or sorted(indexes) != list(
                range(len(candidates))
            ):
                raise ValueError("rerank 返回索引缺失、重复或越界")
            if any(
                type(r["relevance_score"]) not in {float, int}
                or not math.isfinite(r["relevance_score"])
                for r in rows
            ):
                raise ValueError("rerank 返回非法分数")
            usage = Usage(
                input_tokens=data.get("usage", {}).get("prompt_tokens"),
                output_tokens=0,
                total_tokens=data.get("usage", {}).get("total_tokens"),
            )
            return [
                RankedCandidate(
                    candidates[r["index"]].object_id,
                    candidates[r["index"]].text,
                    r["relevance_score"],
                )
                for r in sorted(rows, key=lambda r: -r["relevance_score"])
            ]
        except Exception as exc:
            error = type(exc).__name__
            # 不让供应商正文或带凭据的请求进入日志。
            raise RuntimeError("重排失败：" + error) from None
        finally:
            self.record(
                ModelInvocation(
                    invocation_id=new_id("inv"),
                    run_id=self.run_id,
                    stage="rerank",
                    provider=self.endpoint.base_url,
                    model=self.endpoint.model,
                    usage=usage,
                    error=error,
                    timing=Timing(duration_ms=(perf_counter() - started) * 1000),
                )
            )
