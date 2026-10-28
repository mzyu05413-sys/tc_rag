"""定义外部能力的类型契约，不依赖具体供应商。"""

from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from tc_rag.domain.models import Asset, Document, DocumentBlock, Issue, Usage
from tc_rag.domain.review import PointTree

T = TypeVar("T", bound=BaseModel)

# dataclass 表达跨边界传输的数据；Protocol 表达外部能力的方法契约。
# 领域事实仍使用 domain 模型，不在这里引入供应商 SDK 或数据库实现。


@dataclass(frozen=True)
class Capabilities:
    structured_output: bool = False
    images: bool = False
    batch: bool = False
    max_input_tokens: int | None = None


@dataclass
class ParsedDocument:
    blocks: list[DocumentBlock]
    issues: list[Issue] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)


@dataclass
class ParseState:
    """跨页累计状态；可序列化为按页断点检查点。"""

    heading_sizes: list[float]
    headings: dict[int, str] = field(default_factory=dict)
    order: int = 0
    issue_count: int = 0
    font_warnings: int = 0

    def to_payload(self) -> dict[str, Any]:
        # JSON 键只能是字符串，标题层级用有序二元组保存，恢复时再转回 int。
        return {
            "heading_sizes": list(self.heading_sizes),
            "headings": [[level, title] for level, title in sorted(self.headings.items())],
            "order": self.order,
            "issue_count": self.issue_count,
            "font_warnings": self.font_warnings,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ParseState":
        return cls(
            heading_sizes=[float(size) for size in payload["heading_sizes"]],
            headings={int(level): str(title) for level, title in payload["headings"]},
            order=int(payload["order"]),
            issue_count=int(payload["issue_count"]),
            font_warnings=int(payload["font_warnings"]),
        )


@dataclass
class PageOutcome:
    """单页解析结果与该页解析后的累计状态。"""

    page_no: int
    blocks: list[DocumentBlock]
    assets: list[Asset]
    issues: list[Issue]
    state: ParseState

    def to_payload(self) -> dict[str, Any]:
        return {
            "page_no": self.page_no,
            "blocks": [block.model_dump(mode="json") for block in self.blocks],
            "assets": [asset.model_dump(mode="json") for asset in self.assets],
            "issues": [item.model_dump(mode="json") for item in self.issues],
            "state": self.state.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PageOutcome":
        return cls(
            page_no=int(payload["page_no"]),
            blocks=[DocumentBlock.model_validate(item) for item in payload["blocks"]],
            assets=[Asset.model_validate(item) for item in payload["assets"]],
            issues=[Issue.model_validate(item) for item in payload["issues"]],
            state=ParseState.from_payload(payload["state"]),
        )


class PageSession(Protocol):
    """按页解析会话；只暴露页码与单页解析，不暴露底层文档对象。"""

    @property
    def page_count(self) -> int: ...

    def parse_page(self, page_index: int, state: ParseState) -> PageOutcome: ...


class PagedDocumentParser(Protocol):
    """支持按页断点续跑的解析器（目前为 PDF）。"""

    def session(self, asset: Asset) -> AbstractContextManager[PageSession]: ...


@dataclass
class ModelResult:
    # usage 未知时保留 None（Usage 的默认值），不能把未知消耗写成零。
    output: BaseModel
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class EmbeddingSpace:
    # 空间标识包含所有影响向量语义的参数；登记与缓存由检索适配器持久化。
    provider: str
    model: str
    dimension: int
    normalize: bool = True
    template_version: str = "v1"


@dataclass
class EmbeddingBatch:
    vectors: list[list[float]]
    space: EmbeddingSpace
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class SearchHit:
    object_id: str
    raw_score: float
    metric: str
    score_kind: str
    rank: int
    index_version: str


@dataclass(frozen=True)
class RankedCandidate:
    object_id: str
    text: str
    score: float | None = None


@dataclass(frozen=True)
class VisionResult:
    text: str
    model: str
    version: str


class DocumentParser(Protocol):
    def parse(self, asset: Asset) -> ParsedDocument: ...


class StructuredLLM(Protocol):
    # 实现类只需满足结构，不必显式继承此协议；mypy 用它检查可替换性。
    capabilities: Capabilities

    async def generate(self, request: dict[str, Any], output_schema: type[T]) -> ModelResult: ...


class Embedder(Protocol):
    capabilities: Capabilities

    async def embed_batch(self, texts: list[str], space: EmbeddingSpace) -> EmbeddingBatch: ...


class Reranker(Protocol):
    async def rank(
        self, query: str, candidates: list[RankedCandidate]
    ) -> list[RankedCandidate]: ...


class VisionReader(Protocol):
    async def read(self, image: bytes, context: str) -> VisionResult: ...


class Repository(Protocol):
    # 上层传入领域对象，具体适配器负责序列化和持久化。
    def save(self, collection: str, object_id: str, value: BaseModel) -> None: ...

    def get(self, collection: str, object_id: str, schema: type[T]) -> T | None: ...

    def save_many(self, records: Iterable[tuple[str, str, BaseModel]]) -> None: ...

    def delete(self, collection: str, object_id: str) -> None: ...

    def find_document(self, content_hash: str) -> Document | None: ...

    def allocate_vector_ids(self, space_id: str, object_ids: list[str]) -> list[int]: ...


class ArtifactStore(Protocol):
    def put(self, relative_path: str, content: bytes) -> Asset: ...

    def read(self, asset: Asset) -> bytes: ...


class VectorIndex(Protocol):
    def build(
        self, object_ids: list[str], vectors: list[list[float]], space: EmbeddingSpace
    ) -> None: ...

    def search(
        self, vector: list[float], top_k: int, allowed_ids: set[str] | None = None
    ) -> list[SearchHit]: ...


class RetrievalContext(Protocol):
    async def prepare(self, document_fingerprint: str, evidence: list[Any]) -> None: ...

    async def retrieve(self, query: str, direct_ids: set[str]) -> dict[str, Any]: ...


class Exporter(Protocol):
    # 导出只是事实数据的视图，不应反向修改业务事实。
    def export(self, payload: dict[str, Any], destination: Path) -> Path: ...


class PointMapAdapter(Protocol):
    """测试点目录导出边界，不承载测试用例步骤或审核回导。"""

    def export(self, tree: PointTree, destination: Path) -> Path: ...
