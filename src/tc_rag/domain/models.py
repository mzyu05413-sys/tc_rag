"""P1 data contracts. Cross-object evidence checks belong to application services."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["1.0"] = "1.0"


class Locator(DomainModel):
    """PDF pages are one-based; DOCX order and character offsets are zero-based."""

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
    block_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: Literal["heading", "paragraph", "table", "image", "other"]
    heading_path: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    content: str
    locator: Locator


class ChunkSource(DomainModel):
    """Half-open character interval [start_char, end_char) in the source block."""

    block_id: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)

    @model_validator(mode="after")
    def check_interval(self) -> "ChunkSource":
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
    evidence_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    locator: Locator
    quote: str = Field(min_length=1)
    origin: Literal["text", "ocr", "vision"] = "text"
    processing_model: str | None = None
    processing_version: str | None = None

    @model_validator(mode="after")
    def check_provenance(self) -> "EvidenceSpan":
        if self.origin != "text" and not (self.processing_model and self.processing_version):
            raise ValueError("OCR/vision evidence requires processing model and version")
        return self


class Feature(DomainModel):
    feature_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    evidence_ids: list[str] = Field(min_length=1)


class StructuredConstraint(DomainModel):
    field_path: str | None = None
    operator: str | None = None
    boundary: Any = None
    precision: int | None = Field(default=None, ge=0)
    unit: str | None = None
    applicable_when: str | None = None


class RequirementRule(DomainModel):
    rule_id: str = Field(min_length=1)
    feature_id: str = Field(min_length=1)
    kind: Literal["field_constraint", "business_condition", "permission", "state_transition",
                  "error_behavior", "workflow", "other"]
    statement: str = Field(min_length=1)
    structured_constraint: StructuredConstraint | None = None
    evidence_ids: list[str] = Field(min_length=1)
    status: Literal["supported", "ambiguous", "conflicting", "needs_review"] = "needs_review"


class TestPoint(DomainModel):
    __test__ = False
    point_id: str = Field(min_length=1)
    rule_ids: list[str] = Field(min_length=1)
    technique: Literal["equivalence", "boundary", "decision_table", "state", "permission",
                       "workflow", "error_behavior", "other"]
    objective: str = Field(min_length=1)
    applicability: Literal["applicable", "not_applicable", "undetermined"] = "undetermined"
    status: Literal["ready", "needs_context", "needs_clarification", "not_applicable"] = (
        "needs_clarification")


class TestStep(DomainModel):
    __test__ = False
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    rule_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class TestCase(DomainModel):
    __test__ = False
    case_id: str = Field(min_length=1)
    point_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    steps: list[TestStep] = Field(min_length=1)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    status: Literal["draft", "validated", "needs_review", "superseded"] = "draft"


class CoverageLink(DomainModel):
    case_id: str = Field(min_length=1)
    step_no: int = Field(ge=1)
    rule_id: str = Field(min_length=1)
    point_id: str = Field(min_length=1)
    validation_status: Literal["pending", "validated", "invalid", "needs_review"] = "pending"


class Issue(DomainModel):
    issue_id: str = Field(min_length=1)
    kind: Literal["gap", "conflict", "validation", "unsupported", "unsupported_expectation",
                  "duplicate", "coverage", "other"]
    object_ids: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "error"]
    description: str = Field(min_length=1)
    resolution: str | None = None


class Timing(DomainModel):
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Run(DomainModel):
    run_id: str = Field(min_length=1)
    config_snapshot: dict[str, Any]
    status: Literal["queued", "running", "succeeded", "partial_failed", "failed", "cancelled"]
    timing: Timing = Field(default_factory=Timing)


class Usage(DomainModel):
    """None means unknown, rather than zero consumption."""

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
