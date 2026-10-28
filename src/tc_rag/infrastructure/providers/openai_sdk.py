"""OpenAI 兼容适配器：SDK 重试关闭，逐次请求记录、限并发和有界修复。"""

import asyncio
import json
import random
from collections.abc import Callable
from time import perf_counter
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import ValidationError

from tc_rag.application.ports.contracts import Capabilities, ModelResult, T
from tc_rag.application.ports.errors import OutputTruncatedError
from tc_rag.application.progress import ProgressEvent, ProgressReporter, report
from tc_rag.core.config import Endpoint, Runtime, resolve_api_key
from tc_rag.domain.identity import new_id
from tc_rag.domain.models import ModelInvocation, Timing, Usage


class ProviderError(RuntimeError):
    """只暴露错误类型，不把供应商响应正文或请求中的需求内容写入日志。"""


class SchemaOutputError(ProviderError):
    """HTTP 成功但结构失败，单独计入修复统计。"""

    def __init__(self, details: list[dict[str, Any]]) -> None:
        self.details = details
        super().__init__("模型输出结构无效")


def _schema_errors(exc: ValidationError, schema: type[T]) -> list[dict[str, Any]]:
    """修复只携带契约字段名与错误类型，过滤输出内容和任意未知字段名。"""
    names: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            names.update(node.get("properties", {}))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    contract = schema.model_json_schema()
    collect(contract)

    def node_at(path: tuple[str | int, ...]) -> dict[str, Any]:
        node = contract
        for part in (*path, None):
            while isinstance(node, dict) and "$ref" in node:
                ref = str(node["$ref"])
                node = contract.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})
            if part is None:
                break
            if not isinstance(node, dict):
                return {}
            if isinstance(part, int):
                node = node.get("items", {})
            else:
                node = node.get("properties", {}).get(part, node.get("additionalProperties", {}))
        return node if isinstance(node, dict) else {}

    details = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False)[:20]:
        detail: dict[str, Any] = {
            "path": [
                part if isinstance(part, int) or part in names else "<unknown_field>"
                for part in error["loc"]
            ],
            "error": error["type"],
        }
        expected = node_at(error["loc"])
        if "const" in expected:
            detail["allowed_values"] = [expected["const"]]
        elif "enum" in expected:
            detail["allowed_values"] = expected["enum"]
        elif "type" in expected:
            detail["expected_type"] = expected["type"]
        if error["type"] == "extra_forbidden":
            parent = node_at(error["loc"][:-1])
            detail["allowed_fields"] = sorted(parent.get("properties", {}))
        if error["msg"] == (
            "Value error, OCR/vision evidence requires processing model and version"
        ):
            detail["constraint"] = "Copy processing_model and processing_version from source block."
        id_constraints = {
            f"Value error, Extraction {kind} IDs must be unique": kind
            for kind in ("evidence", "feature", "rule", "entity", "issue")
        }
        if error["msg"] in id_constraints:
            kind = id_constraints[error["msg"]]
            detail["constraint"] = (
                f"Each {kind} object must have a unique ID within its collection. "
                "Use distinct prefixes e_, f_, r_, entity_, issue_ to avoid ambiguous references. "
                "Update all references consistently; rule_mapping must remain {}."
            )
        details.append(detail)
    return details


class OpenAIStructuredLLM:
    capabilities = Capabilities(structured_output=True)

    def __init__(
        self,
        endpoint: Endpoint,
        runtime: Runtime,
        run_id: str,
        record: Callable[[ModelInvocation], None],
        *,
        repairs: int = 1,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if endpoint.adapter != "openai_sdk" or not endpoint.enabled or not endpoint.model:
            raise ValueError("生成模型需要启用 openai_sdk 并配置模型名称")
        key = resolve_api_key(endpoint)
        if client is None and not key:
            raise ValueError("生成模型缺少 api_key 或有效的密钥环境变量")
        if endpoint.base_url and ("@" in endpoint.base_url or "?" in endpoint.base_url):
            raise ValueError("base_url 不允许包含凭据或查询参数")
        self.client = client or AsyncOpenAI(
            api_key=key,
            base_url=endpoint.base_url or None,
            max_retries=0,
            timeout=runtime.request_timeout_seconds,
        )
        self.endpoint, self.runtime = endpoint, runtime
        self.run_id, self.record, self.repairs = run_id, record, repairs
        self.semaphore = asyncio.Semaphore(endpoint.concurrency)
        self.calls = 0
        self.admit: Callable[[], None] | None = None
        self.progress: ProgressReporter | None = None

    async def close(self) -> None:
        await self.client.close()

    async def generate(self, request: dict[str, Any], output_schema: type[T]) -> ModelResult:
        if self.runtime.budget.max_cost is not None:
            raise ValueError("尚未配置价格表，无法执行金额预算；请使用 max_calls")
        payload = dict(request)
        for repair in range(self.repairs + 1):
            try:
                result = await self._request(payload, output_schema)
                output = output_schema.model_validate_json(result[0])
                return ModelResult(output=output, usage=result[1], request_id=result[2])
            except SchemaOutputError as exc:
                if repair == self.repairs:
                    raise ProviderError(
                        "模型输出未通过 JSON/Schema 校验；字段问题："
                        + json.dumps(exc.details, ensure_ascii=False)
                    ) from None
                # 不重放错误输出；给出安全的字段路径，帮助模型修正同一份资料的结构。
                payload["repair"] = "上次输出未通过结构校验，请重新输出符合 schema 的完整 JSON。"
                if request.get("stage") == "extract":
                    payload["repair"] += (
                        "本阶段只提炼需求，禁止输出测试点或用例；"
                        "rule_mapping 由程序合并时计算，此处必须为空对象 {}。"
                    )
                payload["validation_errors"] = exc.details
                report(
                    self.progress,
                    ProgressEvent(
                        "repair", str(request.get("stage", "unknown")), attempt=repair + 1
                    ),
                )
        raise AssertionError("unreachable")

    async def _request(
        self,
        request: dict[str, Any],
        schema: type[T],
    ) -> tuple[str, Usage, str]:
        async with self.semaphore:
            for attempt in range(self.runtime.max_attempts):
                if (
                    self.runtime.budget.max_calls is not None
                    and self.calls >= self.runtime.budget.max_calls
                ):
                    raise ProviderError("达到模型调用次数预算")
                self.calls += 1
                if self.admit:
                    self.admit()
                started = perf_counter()
                report(
                    self.progress,
                    ProgressEvent("model_started", str(request.get("stage", "unknown"))),
                )
                usage = Usage()
                error: str | None = None
                delay = min(2**attempt + random.random(), 30)
                try:
                    # 动态 schema 通过 SDK 传递；输出仍在本地执行严格领域校验。
                    client: Any = self.client
                    schema_format = {
                        "type": "json_schema",
                        "name": schema.__name__,
                        "strict": False,
                        "schema": schema.model_json_schema(),
                    }
                    user_payload = {k: v for k, v in request.items() if k != "system"}
                    chat_format: dict[str, Any]
                    responses_format: dict[str, Any]
                    if self.endpoint.response_format == "json_object":
                        # 部分兼容服务不支持远端 JSON Schema 约束，须将完整契约随输入发送。
                        # 这是协议编码差异，不放宽下方本地结构校验与后续证据引用校验。
                        user_payload["output_schema"] = schema_format["schema"]
                        user_payload["output_format"] = "JSON"
                        chat_format = {"type": "json_object"}
                        responses_format = {"type": "json_object"}
                    else:
                        chat_format = {
                            "type": "json_schema",
                            "json_schema": {k: v for k, v in schema_format.items() if k != "type"},
                        }
                        responses_format = schema_format
                    messages = [
                        {"role": "system", "content": str(request.get("system", ""))},
                        {
                            "role": "user",
                            "content": json.dumps(
                                user_payload,
                                ensure_ascii=False,
                            ),
                        },
                    ]
                    async with asyncio.timeout(self.runtime.request_timeout_seconds):
                        if self.endpoint.api_mode == "responses":
                            response_options: dict[str, Any] = {}
                            if self.endpoint.max_output_tokens is not None:
                                response_options["max_output_tokens"] = (
                                    self.endpoint.max_output_tokens
                                )
                            response = await client.responses.create(
                                model=self.endpoint.model,
                                input=messages,
                                text={"format": responses_format},
                                store=False,
                                **response_options,
                            )
                            if response.usage is not None:
                                usage = Usage(
                                    input_tokens=response.usage.input_tokens,
                                    output_tokens=response.usage.output_tokens,
                                    total_tokens=response.usage.total_tokens,
                                )
                            if response.status == "incomplete":
                                raise OutputTruncatedError("模型输出未完成")
                            content, request_id = response.output_text, response.id
                        else:
                            options: dict[str, Any] = {}
                            if self.endpoint.max_output_tokens is not None:
                                options["max_tokens"] = self.endpoint.max_output_tokens
                            if self.endpoint.thinking_mode != "default":
                                options["extra_body"] = {
                                    "thinking": {"type": self.endpoint.thinking_mode}
                                }
                            response = await client.chat.completions.create(
                                model=self.endpoint.model,
                                messages=messages,
                                response_format=chat_format,
                                **options,
                            )
                            if response.usage is not None:
                                usage = Usage(
                                    input_tokens=response.usage.prompt_tokens,
                                    output_tokens=response.usage.completion_tokens,
                                    total_tokens=response.usage.total_tokens,
                                )
                            if not response.choices:
                                raise ProviderError("供应商未返回候选输出")
                            choice = response.choices[0]
                            if choice.finish_reason == "length":
                                raise OutputTruncatedError("模型输出达到长度限制")
                            if choice.message.refusal:
                                raise ProviderError("供应商拒绝生成")
                            content, request_id = choice.message.content or "", response.id
                    try:
                        schema.model_validate_json(str(content))
                    except ValidationError as exc:
                        raise SchemaOutputError(_schema_errors(exc, schema)) from None
                    return str(content), usage, str(request_id)
                except APIStatusError as exc:
                    error = f"HTTP_{exc.status_code}"
                    if exc.status_code not in {408, 409, 429} and exc.status_code < 500:
                        raise ProviderError(error) from None
                    header = exc.response.headers.get("retry-after", "")
                    try:
                        delay = max(0, min(float(header), 60)) if header else delay
                    except ValueError:
                        pass
                    if attempt + 1 == self.runtime.max_attempts:
                        raise ProviderError(error) from None
                except (APIConnectionError, TimeoutError) as exc:
                    error = type(exc).__name__
                    if attempt + 1 == self.runtime.max_attempts:
                        raise ProviderError(error) from None
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    report(
                        self.progress,
                        ProgressEvent(
                            "model_finished",
                            str(request.get("stage", "unknown")),
                            duration_ms=(perf_counter() - started) * 1000,
                        ),
                    )
                    self.record(
                        ModelInvocation(
                            invocation_id=new_id("inv"),
                            run_id=self.run_id,
                            stage=str(request.get("stage", "unknown")),
                            provider="openai_sdk",
                            model=self.endpoint.model,
                            usage=usage,
                            timing=Timing(duration_ms=(perf_counter() - started) * 1000),
                            error=error,
                        )
                    )
                report(
                    self.progress,
                    ProgressEvent(
                        "retry",
                        str(request.get("stage", "unknown")),
                        attempt=attempt + 1,
                        total=self.runtime.max_attempts,
                    ),
                )
                await asyncio.sleep(delay)
        raise ProviderError("请求重试结束")
