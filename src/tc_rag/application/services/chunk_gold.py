"""分块边界标注契约：绑定原文版本，只有人工 confirmed 单元进入完整率分母。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tc_rag.application.services.chunk_coverage import indexed_ranges, missing_ranges
from tc_rag.application.services.chunking import _EXCEPTION, _table_rows
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Chunk, DocumentBlock


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ReviewRange(ReviewModel):
    block_id: str = Field(min_length=1)
    start_char: int = Field(ge=0, strict=True)
    end_char: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1)


class AtomicUnit(ReviewModel):
    unit_id: str = Field(min_length=1)
    status: Literal["pending", "confirmed", "excluded"] = "pending"
    description: str = ""
    sources: list[ReviewRange] = Field(min_length=1)
    note: str = ""


class ChunkGold(ReviewModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1)
    block_fingerprint: str = Field(min_length=1)
    scope: Literal["sampled_atomic_requirements"] = "sampled_atomic_requirements"
    units: list[AtomicUnit] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_units(self) -> "ChunkGold":
        if len({u.unit_id for u in self.units}) != len(self.units):
            raise ValueError("标注单元 ID 重复")
        return self


def block_fingerprint(blocks: list[DocumentBlock]) -> str:
    return fingerprint([b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.order)])


def validate_gold(gold: ChunkGold, run_id: str, blocks: list[DocumentBlock]) -> None:
    """即便是 pending，也拒绝错误原文/区间，不能把标错的材料悄悄跳过。"""
    if gold.run_id != run_id or gold.block_fingerprint != block_fingerprint(blocks):
        raise ValueError("标注不属于本次运行的原文版本")
    by = {b.block_id: b for b in blocks}
    if len(by) != len(blocks):
        raise ValueError("原文 Block ID 重复")
    for unit in gold.units:
        documents = set()
        spans: dict[str, list[tuple[int, int]]] = {}
        for source in unit.sources:
            block = by.get(source.block_id)
            if block is None or block.kind in {"image", "other"} or not (
                0 <= source.start_char < source.end_char <= len(block.content)
            ):
                raise ValueError("标注来源不存在或字符区间无效")
            if source.quote != block.content[source.start_char:source.end_char]:
                raise ValueError("标注引文与原文区间不一致")
            documents.add(block.document_id)
            spans.setdefault(source.block_id, []).append((source.start_char, source.end_char))
        if len(documents) != 1:
            raise ValueError("一个标注单元不能跨文档")
        for ranges in spans.values():
            ordered = sorted(ranges)
            if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                raise ValueError("同一标注单元的来源区间重复或重叠")


def candidate_review(run_id: str, blocks: list[DocumentBlock], limit: int = 12) -> ChunkGold:
    """独立于 target/overlap 取样；完整段落/表格行只是待确认候选，不自动当金标。"""
    if not 1 <= limit <= 100:
        raise ValueError("候选取样数量应为 1～100")
    candidates: list[list[ReviewRange]] = []
    previous: DocumentBlock | None = None
    for block in sorted(blocks, key=lambda b: b.order):
        if block.kind not in {"paragraph", "table"} or not block.content.strip():
            previous = None
            continue
        spans = (_table_rows(block.content) if block.kind == "table" else []) or [
            (0, len(block.content))
        ]
        for start, end in spans:
            source = ReviewRange(
                block_id=block.block_id, start_char=start, end_char=end,
                quote=block.content[start:end],
            )
            attach = bool(
                candidates and previous and previous.document_id == block.document_id
                and previous.heading_path == block.heading_path
                and previous.kind == block.kind == "paragraph"
                and _EXCEPTION.match(source.quote.strip())
            )
            if attach:
                candidates[-1].append(source)
            else:
                candidates.append([source])
        previous = block
    count = min(limit, len(candidates))
    indexes = ([i * (len(candidates) - 1) // (count - 1) for i in range(count)]
               if count > 1 else [0] if count else [])
    return ChunkGold(
        run_id=run_id, block_fingerprint=block_fingerprint(blocks),
        units=[AtomicUnit(
            unit_id=f"U{i:03d}", sources=candidates[index],
            description="待确认：是否为一条完整可测试约束；必要时拆分、补充条件/例外或排除",
        ) for i, index in enumerate(indexes, 1)],
    )


def evaluate_boundaries(
    gold: ChunkGold | None, run_id: str, blocks: list[DocumentBlock], chunks: list[Chunk],
) -> dict[str, Any]:
    if gold is not None:
        validate_gold(gold, run_id, blocks)
    indexed, invalid = indexed_ranges(blocks, chunks)
    union: dict[str, list[tuple[int, int]]] = {}
    for spans in indexed.values():
        for block_id, ranges in spans.items():
            union.setdefault(block_id, []).extend(ranges)
    details: list[dict[str, Any]] = []
    by = {b.block_id: b for b in blocks}
    for unit in gold.units if gold else []:
        if unit.status != "confirmed":
            continue
        matches = [key for key, ranges in indexed.items() if all(
            not missing_ranges([(s.start_char, s.end_char)], ranges.get(s.block_id, []))
            for s in unit.sources
        )]
        covered = all(not missing_ranges(
            [(s.start_char, s.end_char)], union.get(s.block_id, [])
        ) for s in unit.sources)
        details.append({
            "unit_id": unit.unit_id, "matching_chunks": matches,
            "complete_in_one_chunk": bool(matches), "covered_across_chunks": covered,
            "origins": sorted({by[s.block_id].origin for s in unit.sources}),
        })
    count = len(details)
    complete = sum(d["complete_in_one_chunk"] for d in details)
    groups: dict[str, dict[str, Any]] = {}
    for item in details:
        name = item["origins"][0] if len(item["origins"]) == 1 else "mixed"
        group = groups.setdefault(name, {"confirmed_units": 0, "complete_units": 0})
        group["confirmed_units"] += 1
        group["complete_units"] += item["complete_in_one_chunk"]
        group["boundary_completeness"] = group["complete_units"] / group["confirmed_units"]
    return {
        "confirmed_units": count,
        "pending_units": sum(u.status == "pending" for u in gold.units) if gold else 0,
        "excluded_units": sum(u.status == "excluded" for u in gold.units) if gold else 0,
        "complete_units": complete, "boundary_completeness": complete / count if count else None,
        "split_but_covered_units": sum(
            d["covered_across_chunks"] and not d["complete_in_one_chunk"] for d in details
        ),
        "missing_units": sum(not d["covered_across_chunks"] for d in details),
        "invalid_chunks": invalid, "units": details,
        "origin_groups": groups,
        "scope": "人工确认的抽样需求单元；不能冒充整篇需求召回率或用例质量",
    }
