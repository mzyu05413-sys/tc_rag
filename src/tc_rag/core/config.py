"""Configuration loading; credentials remain environment references."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Section(BaseModel):
    # extra="forbid": 配置里出现未定义字段直接报错（防拼错键名后被静默忽略）
    # hide_input_in_errors=True: 校验报错不回显输入值（配合 api_key_env 防密钥泄漏进错误信息）
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Chunking(Section):
    strategy: Literal["structural"] = "structural"
    target_tokens: int = Field(default=900, gt=0)
    overlap_tokens: int = Field(default=100, ge=0)
    token_counter: Literal["tiktoken_approx"] = "tiktoken_approx"

    # model_validator(mode="after"): 单字段约束表达不了"两者的关系"，需整对象构建后再校验
    @model_validator(mode="after")
    def check_overlap(self) -> "Chunking":
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be less than target_tokens")
        return self


class Endpoint(Section):
    adapter: Literal["openai_sdk", "langchain", "claude_sdk", "http"] = "openai_sdk"
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
    base_url: str = ""
    model: str = ""
    # api_key_env 只允许是合法的环境变量名（或空），不允许塞任意字符串——
    # 这把"密钥值硬写进配置/YAML"从源头挡掉；真实密钥永远只在运行时从环境变量读取（设计书 §17）
    api_key_env: str = Field(default="", pattern=r"^$|^[A-Za-z_][A-Za-z0-9_]*$")
    concurrency: int = Field(default=3, gt=0)
    batch_size: int = Field(default=16, gt=0)
    enabled: bool = True


class Models(Section):
    # 各端点必须用 default_factory 生成独立实例，否则会共享同一可变默认值（pydantic 可变默认陷阱）
    # vision/rerank 默认 disabled：这两类能力是 P2 才启用，P1 不参与主线
    llm: Endpoint = Field(default_factory=lambda: Endpoint(api_key_env="TC_RAG_LLM_API_KEY"))
    embedding: Endpoint = Field(default_factory=lambda: Endpoint(
        api_key_env="TC_RAG_EMBEDDING_API_KEY", concurrency=2))
    vision: Endpoint = Field(default_factory=lambda: Endpoint(enabled=False))
    rerank: Endpoint = Field(default_factory=lambda: Endpoint(enabled=False, adapter="http"))


class Retrieval(Section):
    mode: Literal["direct", "rag"] = "direct"
    scope: Literal["requirement_set"] = "requirement_set"
    backend: Literal["numpy", "faiss"] = "numpy"
    metric: Literal["cosine", "euclidean", "dot"] = "cosine"
    normalize: bool = True
    vector_top_k: int = Field(default=20, gt=0)
    keyword_enabled: bool = False
    rerank_top_n: int = Field(default=6, gt=0)
    threshold_policy: Literal["disabled"] = "disabled"


class Budget(Section):
    max_calls: int | None = Field(default=None, gt=0)
    max_cost: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class Runtime(Section):
    max_attempts: int = Field(default=3, gt=0)
    request_timeout_seconds: float = Field(default=90, gt=0, allow_inf_nan=False)
    cache_enabled: bool = True
    budget: Budget = Field(default_factory=Budget)


# Simple sections are defined as typed schemas, with unknown fields forbidden.
class Project(Section):
    name: str = 'demo'
    workdir: str = './workdir'
class Storage(Section):
    backend: Literal['sqlite'] = 'sqlite'
    database: str = './workdir/data.sqlite'
class Parsing(Section):
    formats: list[Literal['pdf', 'docx']] = ['pdf', 'docx']
    scan_policy: Literal['report_unsupported', 'ocr'] = 'report_unsupported'
    vision_enabled: bool = False
class Planning(Section):
    methods: list[Literal['equivalence', 'boundary', 'decision_table', 'state', 'permission']] = ['equivalence', 'boundary', 'decision_table', 'state', 'permission']
    relation_enabled: bool = False
class Generation(Section):
    batch_point_limit: int = Field(default=5, gt=0)
    schema_repair_attempts: int = Field(default=1, ge=0)
    unsupported_expectation_policy: Literal['flag'] = 'flag'
class Observability(Section):
    metrics_enabled: bool = True
    save_raw_model_payloads: bool = False
class Export(Section):
    formats: list[Literal['json', 'markdown', 'xlsx']] = ['json', 'markdown']
    include_issues: bool = True
    include_coverage: bool = True


class AppConfig(Section):
    project: Project = Field(default_factory=Project)
    storage: Storage = Field(default_factory=Storage)
    parsing: Parsing = Field(default_factory=Parsing)
    chunking: Chunking = Field(default_factory=Chunking)
    models: Models = Field(default_factory=Models)
    retrieval: Retrieval = Field(default_factory=Retrieval)
    planning: Planning = Field(default_factory=Planning)
    generation: Generation = Field(default_factory=Generation)
    runtime: Runtime = Field(default_factory=Runtime)
    observability: Observability = Field(default_factory=Observability)
    export: Export = Field(default_factory=Export)


def _merge(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """深度合并：层配置只覆盖它提到的键，其余保留更早层的默认（而非整块替换）。"""
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    """递归把 ${VAR} 展开为环境变量值；未设置的必填变量直接报错（不静默留空/不解析）。"""
    if isinstance(value, dict):
        return {key: _expand(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, env) for item in value]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if not env.get(name):
                raise ValueError(f"Missing environment variable: {name}")
            return env[name]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    return value


def load_config(path: str | Path | None = None, *,
                environ: Mapping[str, str] | None = None,
                cli_overrides: Mapping[str, Any] | None = None) -> AppConfig:
    """Merge defaults, file, environment, CLI; expand before final validation.

    覆盖优先级（低→高）：内置默认 → YAML 文件 → 环境变量(TC_RAG_*) → CLI 参数。
    environ/cli_overrides 可注入，便于测试不依赖真实环境与文件。
    """
    env = os.environ if environ is None else environ
    # 内置模型默认值作为最低一层（pydantic 字段默认），从它开始逐层覆盖
    data = AppConfig().model_dump()
    if path is not None:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("Configuration root must be a mapping")
        _merge(data, loaded or {})
    # 嵌套式环境变量覆盖：TC_RAG_RETRIEVAL__MODE=rag → retrieval.mode。
    # reversed+逐层包回字典，把扁平名还原成层级结构；值按 YAML 解析以便 "9"→int 9、"true"→bool
    for key, value in env.items():
        if key.startswith("TC_RAG_") and "__" in key:
            nested: Any = yaml.safe_load(value)
            for part in reversed(key[len("TC_RAG_"):].lower().split("__")):
                nested = {part: nested}
            _merge(data, nested)
    # 扁平式环境变量覆盖：TC_RAG_LLM_BASE_URL/... 直接映射到 models.<role>.<field>
    for role in ("llm", "embedding", "vision", "rerank"):
        for field in ("base_url", "model"):
            key = f"TC_RAG_{role.upper()}_{field.upper()}"
            if key in env:
                data["models"][role][field] = env[key]
    # CLI 参数最后覆盖，优先级最高
    _merge(data, cli_overrides or {})
    # 展开 ${VAR} 必须放在 CLI 覆盖之后：CLI 可用真实值替换掉 defaults 里未填的占位，从而避免报缺失
    return AppConfig.model_validate(_expand(data, env))
