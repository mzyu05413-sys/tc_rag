"""加载并校验配置；支持本地密钥，序列化时自动排除密钥值。"""

import os
import re  # 正则校验库
from collections.abc import Mapping  # 类型标注：字典
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class Section(BaseModel):
    # extra="forbid": 配置里出现未定义字段直接报错（防拼错键名后被静默忽略）
    # hide_input_in_errors=True: 校验报错不回显输入值，避免凭据出现在错误说明中。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Chunking(Section):
    """分块参数"""

    strategy: Literal["structural"] = "structural"  # 枚举限制输入
    target_tokens: int = Field(default=900, gt=0)  # 默认值+约束
    overlap_tokens: int = Field(default=100, ge=0)
    token_counter: Literal["tiktoken_approx", "character_approx"] = "tiktoken_approx"

    # model_validator(mode="after"): 单字段约束表达不了"两者的关系"，需整对象构建后再校验
    @model_validator(mode="after")
    def check_overlap(self) -> "Chunking":
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be less than target_tokens")
        return self


class Endpoint(Section):
    """模型端点共用结构"""

    adapter: Literal["openai_sdk", "langchain", "claude_sdk", "http"] = "openai_sdk"
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
    # JSON Object 兼容仅支持 JSON 模式的服务；字段结构仍由本地 Pydantic 严格校验。
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    # 显式选择支持此参数的 Chat 服务；default 不向供应商发送思考开关。
    thinking_mode: Literal["default", "enabled", "disabled"] = "default"
    # 单次输出上限，与 runtime.max_context_tokens 的输入预算分开；留空沿用服务商默认。
    max_output_tokens: int | None = Field(default=None, gt=0)
    base_url: str = ""
    model: str = ""
    # 本地 YAML 可直接填密钥；SecretStr 避免意外显示，exclude 保证快照和导出不含此字段。
    # 模型/配置变更仍参与版本检查；密钥轮换不改变业务配置指纹。
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    # 文件未填写有效密钥时，兼容原有环境变量方式；这里只保存变量名。
    api_key_env: str = Field(default="", pattern=r"^$|^[A-Za-z_][A-Za-z0-9_]*$")
    concurrency: int = Field(default=3, gt=0)
    batch_size: int = Field(default=16, gt=0)
    enabled: bool = True
    supports_images: bool = False  # 开启视觉前必须由部署方明确声明能力


class Models(Section):
    # 各端点必须用 default_factory 生成独立实例，否则会共享同一可变默认值（pydantic 可变默认陷阱）
    # vision/rerank 默认 disabled：这两类能力是 P2 才启用，P1 不参与主线
    llm: Endpoint = Field(default_factory=lambda: Endpoint(api_key_env="TC_RAG_LLM_API_KEY"))
    embedding: Endpoint = Field(
        default_factory=lambda: Endpoint(api_key_env="TC_RAG_EMBEDDING_API_KEY", concurrency=2)
    )
    vision: Endpoint = Field(
        default_factory=lambda: Endpoint(enabled=False, api_key_env="TC_RAG_VISION_API_KEY")
    )
    rerank: Endpoint = Field(
        default_factory=lambda: Endpoint(
            enabled=False, adapter="http", api_key_env="TC_RAG_RERANK_API_KEY"
        )
    )


class Retrieval(Section):
    embedding_dimension: int = Field(default=1536, gt=0)
    """Literal约束"""
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
    max_context_tokens: int = Field(default=12000, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    request_timeout_seconds: float = Field(default=90, gt=0, allow_inf_nan=False)
    # 仅约束视觉真实请求的启动间隔；缓存不等待，不代表服务商承诺的配额。
    vision_min_interval_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    cache_enabled: bool = True
    # fail=任一提炼分块失败即中止；skip=记录问题后跳过该分块继续。
    chunk_failure_policy: Literal["fail", "skip"] = "fail"
    budget: Budget = Field(default_factory=Budget)


# 简单配置分区使用带类型的模型定义，拒绝未声明字段。
class Project(Section):
    name: str = "demo"
    workdir: str = "./workdir"


class Storage(Section):
    backend: Literal["sqlite"] = "sqlite"
    database: str = "./workdir/data.sqlite"


class Parsing(Section):
    formats: list[Literal["pdf", "docx"]] = ["pdf", "docx"]
    # 保留既有配置与快照字段；扫描页仅报告未解析，不再提供独立识别路线。
    scan_policy: Literal["report_unsupported"] = "report_unsupported"
    vision_enabled: bool = False
    # 非标准 Word 标题的保守推断；关闭时保持既有标题样式识别和分块行为。
    infer_docx_headings: bool = False

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        # 新能力默认关闭时不改变旧快照的形状，既有运行仍可用原配置续跑。
        if not self.infer_docx_headings:
            data.pop("infer_docx_headings", None)
        return data


class Planning(Section):
    max_points_per_rule: int = Field(default=12, gt=0)
    methods: list[Literal["equivalence", "boundary", "decision_table", "state", "permission"]] = [
        "equivalence",
        "boundary",
        "decision_table",
        "state",
        "permission",
    ]
    relation_enabled: bool = False
    # merge：同一功能的纯界面展示点合并为一条概括点；keep 保留旧行为。
    ui_display_policy: Literal["merge", "keep"] = "merge"
    # aggregate：仅图片证据不生成点并按功能汇总问题；downgrade/keep 保留旧行为。
    vision_policy: Literal["aggregate", "downgrade", "keep"] = "aggregate"

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        # 默认策略不改变旧快照形状，已有运行仍可按原配置恢复/重做用例。
        if self.ui_display_policy == "merge":
            data.pop("ui_display_policy", None)
        if self.vision_policy == "aggregate":
            data.pop("vision_policy", None)
        return data


class Generation(Section):
    # 默认使用内置用例 Skill；可选自包含 Markdown 追加规范，路径相对启动目录。
    skill_file: str | None = Field(default=None, min_length=1)
    max_cases_per_point: int = Field(default=3, gt=0)
    batch_point_limit: int = Field(default=5, gt=0)
    schema_repair_attempts: int = Field(default=1, ge=0)
    unsupported_expectation_policy: Literal["flag"] = "flag"


class Observability(Section):
    metrics_enabled: bool = True
    save_raw_model_payloads: bool = False


class ExcelColumn(Section):
    field: Literal[
        "title",
        "external_id",
        "preconditions",
        "module_path",
        "actions",
        "expected",
        "tags",
        "remarks",
        "priority",
        "product_version",
        "edit_mode",
    ]
    header: str = Field(min_length=1)


class Export(Section):
    formats: list[Literal["json", "markdown", "xlsx"]] = ["json", "markdown", "xlsx"]
    include_issues: bool = True
    include_coverage: bool = True
    case_columns: list[ExcelColumn] = Field(default_factory=list)
    # 默认按原文章节/模块组织；feature/chapter 保留旧布局，均保留全部点。
    point_tree_layout: Literal["feature", "chapter", "module"] = "module"

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.point_tree_layout == "feature":
            data.pop("point_tree_layout", None)
        return data

    @model_validator(mode="after")
    def unique_columns(self) -> "Export":
        if len({c.field for c in self.case_columns}) != len(self.case_columns):
            raise ValueError("Excel 列字段不能重复")
        return self


class AppConfig(Section):
    """根模型"""

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


def resolve_api_key(endpoint: Endpoint, *, environ: Mapping[str, str] | None = None) -> str:
    """非空配置密钥优先，否则读取环境变量；只在启动校验和客户端装配时使用。"""
    if endpoint.api_key is not None:
        configured = endpoint.api_key.get_secret_value().strip()
        if configured:
            return configured
    env = os.environ if environ is None else environ
    return env.get(endpoint.api_key_env, "").strip()


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


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """按默认值、文件、环境变量、命令行顺序合并，展开变量后执行最终校验。

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
    # 嵌套式环境变量覆盖：TC_RAG_RETRIEVAL__VECTOR_TOP_K=10 → retrieval.vector_top_k。
    # reversed+逐层包回字典，把扁平名还原成层级结构；值按 YAML 解析以便 "9"→int 9、"true"→bool
    for key, value in env.items():
        if key.startswith("TC_RAG_") and "__" in key:
            nested: Any = yaml.safe_load(value)
            for part in reversed(key[len("TC_RAG_") :].lower().split("__")):
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
