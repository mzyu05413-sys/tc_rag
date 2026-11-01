"""测试点目录树；末尾审核模型仅用于兼容已有 SQLite 历史记录。"""

from typing import Literal

from pydantic import Field, model_validator

from tc_rag.domain.models import DomainModel


class PointNode(DomainModel):
    """树使用扁平存储；point_id 关联用例，标题只用于目录展示。"""

    node_id: str = Field(min_length=1)
    parent_id: str | None = None
    title: str = Field(min_length=1)
    kind: Literal["group", "point"]
    order: int = Field(default=0, ge=0)
    point_id: str | None = None
    technique: str | None = None
    status: Literal["ready", "needs_context", "needs_clarification", "not_applicable"] | None = None

    @model_validator(mode="after")
    def check_point_binding(self) -> "PointNode":
        # 分组节点不代表测试点；正式测试点节点必须关联领域对象。
        if self.kind == "point" and not self.point_id:
            raise ValueError("测试点节点必须关联 point_id")
        if self.kind == "group" and self.point_id is not None:
            raise ValueError("分组节点不能关联 point_id")
        return self


class PointTree(DomainModel):
    """一个不可混用的测试点树版本；修改后应创建新版本而非覆盖基准。"""

    tree_id: str = Field(min_length=1)
    version: int = Field(gt=0)
    run_id: str = Field(min_length=1)
    document_fingerprint: str = Field(min_length=1)
    nodes: list[PointNode] = Field(min_length=1)

    @model_validator(mode="after")
    def check_tree(self) -> "PointTree":
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("node_id 不能重复")
        if sum(node.parent_id is None for node in self.nodes) != 1:
            raise ValueError("测试点树必须恰好有一个根节点")
        point_ids = [node.point_id for node in self.nodes if node.kind == "point"]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("同一测试点不能重复绑定多个节点")
        for node in self.nodes:
            if node.parent_id is not None:
                parent = by_id.get(node.parent_id)
                if parent is None or parent.kind != "group":
                    raise ValueError("父节点必须是树中存在的分组")
            # 沿父链回溯，既能发现自环，也能发现与根脱离的循环。
            seen: set[str] = set()
            current: PointNode | None = node
            while current is not None:
                if current.node_id in seen:
                    raise ValueError("测试点树不能有循环")
                seen.add(current.node_id)
                current = by_id.get(current.parent_id) if current.parent_id else None
        return self


class ReviewChange(DomainModel):
    """审核修改先作为草稿承接，允许新增点暂时没有规则依据。"""

    change_id: str = Field(min_length=1)
    action: Literal["add", "update", "delete", "move"]
    node_id: str = Field(min_length=1)
    title: str | None = None
    parent_id: str | None = None
    proposed_rule_ids: list[str] = Field(default_factory=list)
    evidence_status: Literal["pending", "supported", "insufficient"] = "pending"


class ReviewSnapshot(DomainModel):
    """人工决定与技术校验独立；记录审核文件指纹，防止后续换稿而未察觉。"""

    review_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    tree_id: str = Field(min_length=1)
    base_version: int = Field(gt=0)
    document_fingerprint: str = Field(min_length=1)
    reviewed_artifact_checksum: str = Field(min_length=1)
    decision: Literal["pending", "approved", "changes_requested"] = "pending"
    reviewer: str | None = None
    changes: list[ReviewChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_review(self) -> "ReviewSnapshot":
        if self.decision != "pending" and not self.reviewer:
            raise ValueError("人工决定必须记录审核人")
        ids = [change.change_id for change in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("审核变更 ID 不能重复")
        return self
