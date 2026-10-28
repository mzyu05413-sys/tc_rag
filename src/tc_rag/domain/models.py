"""定义 P1 使用的数据契约；跨对象证据校验由应用服务负责。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    # 结构版本与业务文档版本分开；未知字段直接报错，避免拼写问题被静默忽略。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["1.0"] = "1.0"


class Locator(DomainModel):
    """PDF 页码从 1 开始；DOCX 顺序索引和字符偏移从 0 开始。"""

    page: int | None = Field(default=None, ge=1)
    paragraph_index: int | None = Field(default=None, ge=0)
    table_index: int | None = Field(default=None, ge=0)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    bbox: tuple[float, float, float, float] | None = None


class Asset(DomainModel):
    asset_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)


class Document(DomainModel):
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)


class DocumentBlock(DomainModel):
    # 保留原文结构和定位；Chunk 是处理单元，不能取代原始 Block。
    block_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: Literal["heading", "paragraph", "table", "image", "other"]
    heading_path: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    content: str
    locator: Locator
    origin: Literal["text", "ocr", "vision"] = "text"
    processing_model: str | None = None
    processing_version: str | None = None


class ChunkSource(DomainModel):
    """原始文档块中的左闭右开字符区间：[start_char, end_char)。"""

    block_id: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)

    @model_validator(mode="after")
    def check_interval(self) -> "ChunkSource":
        # 这里只能检查区间顺序；是否超出原文长度，要加载对应 Block 后再验证。
        if self.end_char <= self.start_char:
            raise ValueError("end_char must exceed start_char")
        return self


class Chunk(DomainModel):
    chunk_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    token_estimate: int = Field(ge=0)
    fingerprint: str = Field(min_length=1)
    sources: list[ChunkSource] = Field(min_length=1)


class EvidenceSpan(DomainModel):
    # 引文与定位提供追溯入口；有 evidence_id 不等于语义已经获得证明。
    evidence_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    locator: Locator
    quote: str = Field(min_length=1)
    origin: Literal["text", "ocr", "vision"] = "text"
    processing_model: str | None = None
    processing_version: str | None = None
    uncertain: bool = False

    @model_validator(mode="after")
    def check_provenance(self) -> "EvidenceSpan":
        # 模型/OCR 转换结果需要保留处理来源，不与原始文字混为一谈。
        if self.origin != "text" and not (self.processing_model and self.processing_version):
            raise ValueError("OCR/vision evidence requires processing model and version")
        if self.origin == "vision":
            self.uncertain = True
        return self


class Feature(DomainModel):
    feature_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    evidence_ids: list[str] = Field(min_length=1)


class StructuredConstraint(DomainModel):
    # 无可靠依据时保留 None，不猜测边界、精度和单位。
    field_path: str | None = None
    operator: str | None = None
    boundary: Any = None
    precision: int | None = Field(default=None, ge=0)
    unit: str | None = None
    applicable_when: str | None = None


class RequirementRule(DomainModel):
    rule_id: str = Field(min_length=1)
    feature_id: str = Field(min_length=1)
    kind: Literal[
        "field_constraint",
        "business_condition",
        "permission",
        "state_transition",
        "error_behavior",
        "workflow",
        "other",
    ]
    statement: str = Field(min_length=1)
    structured_constraint: StructuredConstraint | None = None
    evidence_ids: list[str] = Field(min_length=1)
    status: Literal["supported", "ambiguous", "conflicting", "needs_review"] = "needs_review"


class TestPoint(DomainModel):
    # 测试意图独立于执行步骤；status 是技术状态，不是人工审核记录。
    __test__ = False
    point_id: str = Field(min_length=1)
    rule_ids: list[str] = Field(min_length=1)
    technique: Literal[
        "equivalence",
        "boundary",
        "decision_table",
        "state",
        "permission",
        "workflow",
        "error_behavior",
        "other",
    ]
    objective: str = Field(min_length=1)
    applicability: Literal["applicable", "not_applicable", "undetermined"] = "undetermined"
    status: Literal["ready", "needs_context", "needs_clarification", "not_applicable"] = (
        "needs_clarification"
    )


class TestStep(DomainModel):
    # 动作与预期成对保存；引用下沉到步骤，后续才能按具体断言计算覆盖。
    __test__ = False
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    rule_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class TestCase(DomainModel):
    # validated 只表示自动校验通过；不能据此认为用户已批准该用例。
    __test__ = False
    case_id: str = Field(min_length=1)
    point_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    steps: list[TestStep] = Field(min_length=1)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    status: Literal["draft", "validated", "needs_review", "superseded"] = "draft"
    # 可选元数据用于目标 Excel 映射，默认值保持既有标注和调用方兼容。
    # 内部 case_id 与外部平台 ID 分开，首次导入时外部 ID 可以留空。
    module_path: str | None = Field(default=None, pattern=r"^/[^/]+(?:/[^/]+)*$")
    tags: list[str] = Field(default_factory=list)
    remarks: str | None = None
    product_version: str | None = None
    external_id: str | None = None
    edit_mode: Literal["STEP"] = "STEP"


class CoverageLink(DomainModel):
    # step_no 从 1 开始，映射一条用例断言对应的规则和测试点。
    case_id: str = Field(min_length=1)
    step_no: int = Field(ge=1)
    rule_id: str = Field(min_length=1)
    point_id: str = Field(min_length=1)
    validation_status: Literal["pending", "validated", "invalid", "needs_review"] = "pending"


class Issue(DomainModel):
    issue_id: str = Field(min_length=1)
    kind: Literal[
        "gap",
        "conflict",
        "validation",
        "unsupported",
        "unparsed",
        "unsupported_expectation",
        "duplicate",
        "coverage",
        "other",
    ]
    object_ids: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "error"]
    description: str = Field(min_length=1)
    resolution: str | None = None


class Timing(DomainModel):
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Run(DomainModel):
    # 快照由应用层提供，调用方必须保证不含密钥；通用字典本身不执行脱敏。
    run_id: str = Field(min_length=1)
    config_snapshot: dict[str, Any]
    status: Literal["queued", "running", "succeeded", "partial_failed", "failed", "cancelled"]
    timing: Timing = Field(default_factory=Timing)


class Usage(DomainModel):
    """None 表示消耗未知，不表示消耗为零。"""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ModelInvocation(DomainModel):
    invocation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    usage: Usage = Field(default_factory=Usage)
    timing: Timing = Field(default_factory=Timing)
    error: str | None = None


class BusinessEntity(DomainModel):
    entity_id: str = Field(min_length=1)
    kind: Literal["field", "role", "state", "action", "other"]
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1)
