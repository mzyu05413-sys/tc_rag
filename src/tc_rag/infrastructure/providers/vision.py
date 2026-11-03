"""显式启用的兼容视觉适配器；预处理参数纳入调用方缓存标识。"""

import asyncio
import base64
import io
import json
import re
from collections.abc import Callable
from math import isfinite
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from PIL import Image, ImageOps

from tc_rag.application.ports.contracts import VisionResult
from tc_rag.application.ports.errors import VisionReadError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.application.services.vision_content import render_description
from tc_rag.core.config import Endpoint, Runtime, VisionProcessing, resolve_api_key
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import ModelInvocation, Timing, Usage
from tc_rag.domain.vision import VisionDescription
from tc_rag.infrastructure.providers.vision_images import ImageView, check_image_size, prepare_views


def validate_vision_endpoint(endpoint: Endpoint) -> None:
    """主读与复核共用能力和 URL 检查，凭据由请求器检查。"""
    if (not endpoint.enabled or not endpoint.supports_images or not endpoint.model
        or endpoint.adapter != "openai_sdk" or endpoint.api_mode != "chat_completions"):
        raise ValueError("vision 需要声明 supports_images 并配置兼容 Chat 模型")
    if "@" in endpoint.base_url or "?" in endpoint.base_url:
        raise ValueError("vision 端点不可包含凭据或查询参数")


def extract_json_object(raw: str) -> str:
    """只去展示包装，不补括号、不修复截断、不合并多个对象。"""
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("响应没有 JSON 对象")
    _, end = json.JSONDecoder().raw_decode(text[start:])
    remainder = text[start + end:].strip()
    if remainder.startswith(("{", "[", "}")):
        raise ValueError("响应包含多个对象或多余括号")
    return text[start:start + end]


class VisionRequester:
    # 智谱这些账户/额度错误无法靠本次短暂重试恢复；其余供应商不套用此约定。
    _account_errors = {
        "1113",
        "1308",
        "1309",
        "1310",
        "1311",
        "1313",
        "1314",
        "1315",
        "1316",
        "1317",
        "1318",
        "1319",
        "1320",
        "1321",
    }

    def __init__(self, endpoint: Endpoint, runtime: Runtime, run_id: str,
                 record: Callable[[ModelInvocation], None], client: AsyncOpenAI | None = None,
                 *, stage: str = "vision"):
        validate_vision_endpoint(endpoint)
        key = resolve_api_key(endpoint)
        if client is None and not key:
            raise ValueError("vision 缺少 api_key 或有效的密钥环境变量")
        self.client = client or AsyncOpenAI(
            api_key=key, base_url=endpoint.base_url or None, max_retries=0,
            timeout=runtime.request_timeout_seconds,
        )
        self.endpoint, self.runtime, self.run_id, self.record = endpoint, runtime, run_id, record
        self.stage = stage
        self.admit: Callable[[], None] | None = None
        self.reserve: Callable[[], None] | None = None
        self.progress: ProgressReporter | None = None
        self.calls = 0
        self.semaphore = asyncio.Semaphore(endpoint.concurrency)
        self._pacing_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._rate_limit_streak = 0
        self._blocked_error: str | None = None

    def _status_code(self, exc: APIStatusError) -> str:
        """只提取可信端点的四位业务码，不保存或显示供应商 message 正文。"""
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

    async def _wait_for_slot(self, *, show_wait: bool) -> None:
        # 同一适配器共享启动间隔和限流冷却，上一张失败不会让下一张立即撞限额。
        async with self._pacing_lock:
            delay = max(0.0, self._next_request_at - perf_counter())
            while delay:
                if show_wait:
                    reason: Literal["pacing", "cooldown"] = (
                        "cooldown" if self._cooldown_until > perf_counter() else "pacing"
                    )
                    report(
                        self.progress,
                        ProgressEvent(
                            "request_wait",
                            "vision",
                            duration_ms=delay * 1000,
                            wait_reason=reason,
                        ),
                    )
                await asyncio.sleep(delay)
                # 等待期间另一个在途请求也可能触发更长冷却，必须重新检查。
                delay = max(0.0, self._next_request_at - perf_counter())
            self._next_request_at = perf_counter() + self.runtime.vision_min_interval_seconds

    async def close(self) -> None:
        await self.client.close()

    def _check_call_budget(self) -> None:
        if (
            self.runtime.budget.max_calls is not None
            and self.calls >= self.runtime.budget.max_calls
        ):
            raise VisionReadError("CALL_BUDGET")

    async def _request(self, prompt: str, content: list[dict[str, Any]]) -> str:
        if self.runtime.budget.max_cost is not None:
            raise ValueError("未配置价格表，不能执行金额预算")
        async with self.semaphore:
            for attempt in range(self.runtime.max_attempts):
                # 每次真实请求（含重试）都占用共享预算，不能交给 SDK 隐式重试。
                self._check_call_budget()
                await self._wait_for_slot(show_wait=attempt == 0)
                # 排队期间其他任务可能消耗预算，必须在真正发请求前再次检查。
                self._check_call_budget()
                if self._blocked_error:
                    raise VisionReadError(self._blocked_error)
                if self.reserve:
                    self.reserve()
                elif self.admit:
                    self.admit()
                self.calls += 1
                started, usage, error = perf_counter(), Usage(), None
                delay = min(2 ** (attempt + 1), 60)
                report(self.progress, ProgressEvent("model_started", "vision"))
                try:
                    options: dict[str, Any] = {}
                    if self.endpoint.max_output_tokens is not None:
                        options["max_tokens"] = self.endpoint.max_output_tokens
                    if self.endpoint.thinking_mode != "default":
                        options["extra_body"] = {"thinking": {"type": self.endpoint.thinking_mode}}
                    if self.endpoint.supports_json_object:
                        options["response_format"] = {"type": "json_object"}
                    options["messages"] = [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": content},
                    ]
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(
                            model=self.endpoint.model,
                            **options,
                        ),
                        self.runtime.request_timeout_seconds,
                    )
                    self._rate_limit_streak = 0
                    if response.usage:
                        usage = Usage(
                            input_tokens=response.usage.prompt_tokens,
                            output_tokens=response.usage.completion_tokens,
                            total_tokens=response.usage.total_tokens,
                        )
                    if not response.choices:
                        raise VisionReadError("EMPTY_RESPONSE")
                    choice = response.choices[0]
                    if (
                        choice.finish_reason != "stop"
                        or not isinstance(choice.message.content, str)
                        or not choice.message.content.strip()
                    ):
                        raise VisionReadError("INCOMPLETE_OUTPUT")
                    return choice.message.content
                except APIStatusError as exc:
                    error = self._status_code(exc)
                    business = error.removeprefix("HTTP_429_BIGMODEL_")
                    if business in self._account_errors:
                        self._blocked_error = error
                        report(
                            self.progress, ProgressEvent("vision_blocked", "vision", error=error)
                        )
                        raise VisionReadError(error) from None
                    if exc.status_code == 429:
                        self._rate_limit_streak += 1
                        # 连续限流跨图片递增：10、20、40、60 秒；成功后重新计数。
                        delay = min(10 * 2 ** min(self._rate_limit_streak - 1, 3), 60)
                    # 等待建议只作为下限，不让较短的 Retry-After 抵消本地退避。
                    try:
                        retry_after = float(exc.response.headers.get("retry-after", ""))
                        if isfinite(retry_after):
                            delay = max(delay, max(0, min(retry_after, 60)))
                    except ValueError:
                        pass
                    if exc.status_code == 429:
                        self._next_request_at = max(self._next_request_at, perf_counter() + delay)
                        self._cooldown_until = max(self._cooldown_until, perf_counter() + delay)
                    if (
                        exc.status_code not in {408, 409, 429} and exc.status_code < 500
                    ) or attempt + 1 == self.runtime.max_attempts:
                        raise VisionReadError(error) from None
                except (APIConnectionError, TimeoutError) as exc:
                    error = type(exc).__name__
                    if attempt + 1 == self.runtime.max_attempts:
                        raise VisionReadError(error) from None
                except VisionReadError as exc:
                    error = exc.code
                    raise
                except asyncio.CancelledError:
                    error = "CancelledError"
                    raise
                except Exception as exc:
                    error = type(exc).__name__
                    raise VisionReadError(error) from None
                finally:
                    report(self.progress, ProgressEvent("model_finished", "vision"))
                    self.record(
                        ModelInvocation(
                            invocation_id=new_id("inv"),
                            run_id=self.run_id,
                            stage=self.stage,
                            provider=self.endpoint.base_url or "openai",
                            model=self.endpoint.model,
                            usage=usage,
                            error=error,
                            timing=Timing(duration_ms=(perf_counter() - started) * 1000),
                        )
                    )
                report(
                    self.progress,
                    ProgressEvent(
                        "retry",
                        "vision",
                        attempt=attempt + 1,
                        total=self.runtime.max_attempts,
                        duration_ms=delay * 1000,
                        error=error,
                    ),
                )
                self._next_request_at = max(self._next_request_at, perf_counter() + delay)
                self._cooldown_until = max(self._cooldown_until, perf_counter() + delay)
        raise VisionReadError("RETRIES_EXHAUSTED")


class OpenAICompatVision(VisionRequester):
    preprocessing = {"max_edge": 1600, "format": "PNG", "version": "v1"}

    def __init__(
        self, endpoint: Endpoint, runtime: Runtime, run_id: str,
        record: Callable[[ModelInvocation], None], prompt: str,
        client: AsyncOpenAI | None = None, *,
        processing: VisionProcessing | None = None, review_prompt: str = "",
        review_endpoint: Endpoint | None = None, review_client: AsyncOpenAI | None = None,
    ):
        if processing is not None and processing.verify and not review_prompt.strip():
            raise ValueError("视觉复核需要独立版本的核对提示词")
        if review_endpoint is not None:
            validate_vision_endpoint(review_endpoint)
            if (endpoint.base_url.rstrip("/"), endpoint.model) == (
                review_endpoint.base_url.rstrip("/"), review_endpoint.model
            ):
                raise ValueError("视觉主模型与复核模型必须使用不同端点或模型")
            if review_client is None and not resolve_api_key(review_endpoint):
                raise ValueError("vision_review 缺少密钥")
        super().__init__(endpoint, runtime, run_id, record, client)
        self.prompt, self.processing, self.review_prompt = prompt, processing, review_prompt
        self.reviewer: VisionRequester | None = None
        self._total_calls = 0
        if processing is not None:
            self.preprocessing = {"version": "v2", "format": "PNG", **processing.model_dump()}
        if review_endpoint is not None:
            self.reviewer = VisionRequester(
                review_endpoint, runtime, run_id, record, review_client, stage="vision_review"
            )
            self.reserve = self.reviewer.reserve = self._reserve_shared

    def _reserve_shared(self) -> None:
        """独立端点各自治理限流，但共用总调用预算和外部 admit。"""
        maximum = self.runtime.budget.max_calls
        if maximum is not None and self._total_calls >= maximum:
            raise VisionReadError("CALL_BUDGET")
        if self.admit:
            self.admit()
        self._total_calls += 1

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self.reviewer is not None and self.reviewer.client is not self.client:
                await self.reviewer.close()

    def check_image(self, image: bytes) -> None:
        if self.processing is not None:
            check_image_size(image, self.processing.min_edge)

    async def read(self, image: bytes, context: str) -> VisionResult:
        if self._blocked_error:
            raise VisionReadError(self._blocked_error)
        if self.processing is not None:
            return await self._read_quality(image, context)
        # 兼容旧端口调用；正式装配显式传入 v2 处理配置。
        # Pillow 自带像素炸弹检查；转换后只发送像素，不携带原文件元数据。
        with Image.open(io.BytesIO(image)) as original:
            converted = ImageOps.exif_transpose(original).convert("RGB")
            converted.thumbnail((1600, 1600))
            buffer = io.BytesIO()
            converted.save(buffer, format="PNG", optimize=True)
        text = await self._request(self.prompt, [
            {"type": "text", "text": context},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
            }},
        ])
        return VisionResult(text, self.endpoint.model, "vision/v1")

    async def _read_quality(self, image: bytes, context: str) -> VisionResult:
        options = self.processing
        assert options is not None
        views = await asyncio.to_thread(prepare_views, image, options)
        try:
            supplied = json.loads(context)
        except ValueError:
            supplied = {"heading_path": [context], "neighbors": []}
        if not isinstance(supplied, dict):
            raise VisionReadError("INVALID_CONTEXT")
        contexts = {item["context_id"] for item in supplied.get("neighbors", [])}
        request = {
            "document_context": supplied,
            "views": [view.metadata() for view in views],
            "output_schema": VisionDescription.model_json_schema(),
        }

        def content(payload: dict[str, Any]) -> list[dict[str, Any]]:
            # 总览与局部图在同一次请求中提供，不通过多次独立描述拼接业务逻辑。
            parts: list[dict[str, Any]] = [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
            ]
            for view in views:
                parts.append({"type": "text", "text": json.dumps(view.metadata())})
                parts.append({"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(view.png).decode(),
                    **({"detail": options.detail} if options.detail is not None else {}),
                }})
            return parts

        draft = await self._request(self.prompt, content(request))
        try:
            parsed = self._parse(draft, views, contexts)
            draft_valid = True
        except ValueError:
            parsed, draft_valid = None, False
        verify = options.verify is True or (
            options.verify == "auto" and (
                parsed is None or parsed.image_type != "ui" or len(views) > 1
                or bool(parsed.uncertainties) or len(parsed.facts) > options.auto_fact_threshold
            )
        )
        verified, verify_error = False, None
        final_model = self.endpoint.model
        if verify:
            try:
                if len(draft) > 100_000:
                    raise VisionReadError("OUTPUT_TOO_LARGE")
                requester = self.reviewer or self
                requester.progress = self.progress
                checked = await requester._request(
                    self.review_prompt,
                    content({**request, "draft": draft, "draft_valid": draft_valid}),
                )
                try:
                    parsed_review = self._parse(checked, views, contexts)
                except ValueError:
                    raise VisionReadError("INVALID_VISUAL_STRUCTURE") from None
                parsed, verified = parsed_review, True
                final_model = requester.endpoint.model
            except VisionReadError as exc:
                if options.verify_failure != "keep_draft" or parsed is None:
                    raise
                verify_error = exc.code
        if parsed is None:
            raise VisionReadError("INVALID_VISUAL_STRUCTURE")
        text = render_description(parsed)
        if verify_error is not None:
            text = "（未复核）\n" + text
        return VisionResult(
            text, final_model, "vision/v2",
            {"description": parsed.model_dump(mode="json"), "draft": draft,
             "draft_valid": draft_valid, "model_verified": verified,
             "human_verified": False, "context": supplied,
             "views": [view.metadata() for view in views],
             **({"verify_error": verify_error} if verify_error is not None else {}),
             **({"review_model": self.reviewer.endpoint.model} if self.reviewer else {})},
        )

    @staticmethod
    def _parse(raw: str, views: list[ImageView], contexts: set[str]) -> VisionDescription:
        description = VisionDescription.model_validate_json(extract_json_object(raw))
        description.check_references({view.view_id for view in views}, contexts)
        return description
