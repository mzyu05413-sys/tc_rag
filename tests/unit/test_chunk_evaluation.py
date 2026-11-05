"""区间守恒与人工原子需求评分，不把候选、重复来源或未知标注算成通过。"""

import json
import random

import pytest
from pydantic import ValidationError

from tc_rag.application.services.chunk_coverage import coverage_diagnostics, missing_ranges
from tc_rag.application.services.chunk_gold import (
    AtomicUnit,
    ChunkGold,
    ReviewRange,
    block_fingerprint,
    candidate_review,
    evaluate_boundaries,
    validate_gold,
)
from tc_rag.application.services.chunking import _table_rows
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock, Issue


def block(name="b", text="abcdef", *, order=0, document="d", kind="paragraph"):
    return DocumentBlock(
        block_id=name, document_id=document, kind=kind, content=text, order=order,
        heading_path=["模块"], locator={},
    )


def chunk(name, blocks, spans):
    by = {b.block_id: b for b in blocks}
    text = "\n".join(by[i].content[a:z] for i, a, z in spans)
    return Chunk(
        chunk_id=name, fingerprint=name, text=text, token_estimate=len(text),
        sources=[ChunkSource(block_id=i, start_char=a, end_char=z) for i, a, z in spans],
    )


def label(blocks, ranges, status="confirmed"):
    by = {b.block_id: b for b in blocks}
    return ChunkGold(
        run_id="run", block_fingerprint=block_fingerprint(blocks), units=[AtomicUnit(
            unit_id="U1", status=status, sources=[ReviewRange(
                block_id=i, start_char=a, end_char=z, quote=by[i].content[a:z],
            ) for i, a, z in ranges],
        )],
    )


def test_source_presence_does_not_hide_hole_inside_paragraph():
    blocks = [block()]
    pieces = [chunk("c", blocks, [("b", 0, 2), ("b", 4, 6)])]
    report = coverage_diagnostics(blocks, pieces, [], 100)
    assert not report["passed"]
    assert report["missing_characters"] == 2
    assert report["missing_ranges"] == [{"block_id": "b", "ranges": [(2, 4)]}]
    assert report["source_character_coverage"] == pytest.approx(4 / 6)
    pieces.append(chunk("c2", blocks, [("b", 1, 5)]))
    assert coverage_diagnostics(blocks, pieces, [], 100)["source_character_coverage"] == 1


def test_table_coverage_uses_exact_rows_and_ignores_only_json_wrapper():
    b = block(text=json.dumps({"rows": [["列", "A"], ["数据", "B"]]}), kind="table")
    ranges = _table_rows(b.content)
    report = coverage_diagnostics([b], [chunk("c", [b], [("b", a, z) for a, z in ranges])], [], 100)
    assert report["passed"] and report["source_character_coverage"] == 1
    assert report["required_characters"] < len(b.content)
    missing_row = coverage_diagnostics([b], [chunk("c", [b], [("b", *ranges[0])])], [], 100)
    assert missing_row["missing_characters"] == ranges[1][1] - ranges[1][0]


@pytest.mark.parametrize("bad", ["text", "range", "duplicate"])
def test_invalid_chunks_cannot_supply_coverage(bad):
    blocks = [block()]
    c = chunk("c", blocks, [("b", 0, 6)])
    chunks = [c]
    if bad == "text":
        c.text = "并非原文"
    elif bad == "range":
        c.sources[0].end_char = 99
    else:
        chunks.append(c.model_copy(deep=True))
    report = coverage_diagnostics(blocks, chunks, [], 100)
    assert not report["passed"] and report["invalid_chunks"]
    assert report["source_character_coverage"] == 0


def test_empty_input_is_unknown_and_oversize_requires_issue():
    assert coverage_diagnostics([], [], [], 100)["source_character_coverage"] is None
    blocks = [block()]
    c = chunk("c", blocks, [("b", 0, 6)])
    assert not coverage_diagnostics(blocks, [c], [], 3)["passed"]
    warning = Issue(issue_id="i", kind="gap", severity="warning", description="保留长句",
                    object_ids=["c"])
    assert coverage_diagnostics(blocks, [c], [warning], 3)["passed"]


def test_range_subtraction_matches_independent_character_set_oracle():
    randomizer = random.Random(411)
    for _ in range(100):
        required, covered = [], []
        for dest in (required, covered):
            for _ in range(5):
                start, end = sorted(randomizer.sample(range(31), 2))
                dest.append((start, end))
        expected = {i for a, z in required for i in range(a, z)} - {
            i for a, z in covered for i in range(a, z)
        }
        actual = {i for a, z in missing_ranges(required, covered) for i in range(a, z)}
        assert actual == expected


def test_boundary_complete_is_different_from_union_coverage_and_counts_adjacent_slices():
    blocks = [block("b1", "如果允许"), block("b2", "则保存", order=1)]
    gold = label(blocks, [("b1", 0, 4), ("b2", 0, 3)])
    split = [chunk("c1", blocks, [("b1", 0, 4)]), chunk("c2", blocks, [("b2", 0, 3)])]
    result = evaluate_boundaries(gold, "run", blocks, split)
    assert result["boundary_completeness"] == 0
    assert result["split_but_covered_units"] == 1 and result["missing_units"] == 0
    together = [chunk("c", blocks, [("b1", 0, 2), ("b1", 2, 4), ("b2", 0, 3)])]
    result = evaluate_boundaries(gold, "run", blocks, together)
    assert result["boundary_completeness"] == 1 and result["complete_units"] == 1
    missing = evaluate_boundaries(gold, "run", blocks, split[:1])
    assert missing["missing_units"] == 1 and missing["split_but_covered_units"] == 0


@pytest.mark.parametrize("status", ["pending", "excluded"])
def test_unreviewed_or_excluded_candidates_do_not_become_gold(status):
    blocks = [block()]
    gold = label(blocks, [("b", 0, 6)], status)
    result = evaluate_boundaries(gold, "run", blocks, [chunk("c", blocks, [("b", 0, 6)])])
    assert result["confirmed_units"] == 0 and result["boundary_completeness"] is None
    assert result[f"{status}_units"] == 1
    assert evaluate_boundaries(None, "run", blocks, [])["boundary_completeness"] is None


@pytest.mark.parametrize("mistake", ["run", "version", "quote", "range", "id", "overlap"])
def test_gold_rejects_wrong_snapshot_or_interval_even_when_pending(mistake):
    blocks = [block()]
    gold = label(blocks, [("b", 0, 6)], "pending")
    source = gold.units[0].sources[0]
    if mistake == "run":
        gold.run_id = "other"
    elif mistake == "version":
        blocks[0].content += "变更"
    elif mistake == "quote":
        source.quote = "自行改写"
    elif mistake == "range":
        source.end_char = 100
    elif mistake == "id":
        source.block_id = "unknown"
    else:
        gold.units[0].sources.append(source.model_copy(deep=True))
    with pytest.raises(ValueError):
        validate_gold(gold, "run", blocks)


def test_gold_rejects_cross_document_unit_unknown_fields_and_duplicate_unit_id():
    blocks = [block("b1"), block("b2", document="other", order=1)]
    gold = label(blocks, [("b1", 0, 6), ("b2", 0, 6)])
    with pytest.raises(ValueError, match="跨文档"):
        validate_gold(gold, "run", blocks)
    raw = label(blocks, [("b1", 0, 6)]).model_dump()
    raw["units"] *= 2
    with pytest.raises(ValidationError):
        ChunkGold.model_validate(raw)
    raw["units"] = raw["units"][:1]
    raw["approved"] = True
    with pytest.raises(ValidationError):
        ChunkGold.model_validate(raw)


def test_candidates_are_deterministic_pending_and_not_derived_from_chunk_boundaries():
    blocks = [block(f"b{i}", f"第{i}段原文。", order=i) for i in range(25)]
    result = candidate_review("run", blocks)
    assert result == candidate_review("run", blocks)
    assert len(result.units) == 12 and all(u.status == "pending" for u in result.units)
    assert result.units[0].sources[0].block_id == "b0"
    assert result.units[-1].sources[0].block_id == "b24"
    validate_gold(result, "run", blocks)
    assert evaluate_boundaries(result, "run", blocks, [])["boundary_completeness"] is None


def test_boundary_metrics_preserve_vision_origin_separately():
    blocks = [block()]
    blocks[0].origin = "vision"
    blocks[0].processing_model, blocks[0].processing_version = "fixture", "v1"
    gold = label(blocks, [("b", 0, 6)])
    report = evaluate_boundaries(gold, "run", blocks, [chunk("c", blocks, [("b", 0, 6)])])
    assert report["origin_groups"] == {"vision": {
        "confirmed_units": 1, "complete_units": 1, "boundary_completeness": 1,
    }}
    assert report["units"][0]["origins"] == ["vision"]
