"""邻文窗口与预算裁减只作用于背景，不动核心证据和原文。"""

import json

import pytest

from tc_rag.application.services.chunking import structural_chunks
from tc_rag.application.services.extraction_context import fit_context_budget, neighboring_context
from tc_rag.domain.models import Chunk, ChunkSource, DocumentBlock


def block(i, *, doc="d", path=None, origin="text"):
    return DocumentBlock(
        block_id=f"b{i}", document_id=doc, kind="paragraph", order=i,
        content=str(i) * 20, heading_path=path or [str(i)], locator={}, origin=origin,
    )


def test_neighbor_windows_cross_section_but_not_document_and_preserve_origin():
    blocks = [block(0, doc="old"), block(1), block(2), block(3, origin="vision"),
              block(4), block(5, doc="other")]
    chunk = structural_chunks([blocks[2]], 100, 0).chunks[0]
    result = neighboring_context(chunk, blocks, 4, 24)
    assert result == neighboring_context(chunk, list(reversed(blocks)), 4, 24)
    assert {r["block_id"] for r in result} == {"b1", "b3", "b4"}
    assert sum(len(r["text"]) for r in result) <= 24
    for r in result:
        b = next(b for b in blocks if b.block_id == r["block_id"])
        assert r["text"] == b.content[r["start_char"]:r["end_char"]]
        assert r["truncated"] and r["role"] == "background_only"
    assert next(r for r in result if r["block_id"] == "b3")["origin"] == "vision"
    assert neighboring_context(chunk, blocks, 0, 24) == []
    assert neighboring_context(chunk, blocks, 2, 0) == []


def test_budget_trimming_keeps_core_and_removes_farthest_background_first():
    context = [{"block_id": f"b{i}", "distance": i, "text": "x" * 100} for i in (1, 3, 2)]
    request = {"chunk": {"text": "必须保留"}, "system": "固定提示", "neighbor_context": context}
    maximum = len(json.dumps({**request, "output_schema": {}}, ensure_ascii=False)) - 1
    fit_context_budget(request, {}, maximum, len)
    assert request["neighbor_context_omitted"][0] == "b3"
    assert request["chunk"] == {"text": "必须保留"} and request["system"] == "固定提示"
    fit_context_budget(request, {}, 0, len)
    assert request["neighbor_context"] == []
    assert request["chunk"]["text"] == "必须保留"
    assert set(request["neighbor_context_omitted"]) == {"b1", "b2", "b3"}


def part(b, ranges):
    return Chunk(
        chunk_id="chunk", fingerprint="fixture", token_estimate=1,
        text="\n".join(b.content[a:z] for a, z in ranges),
        sources=[ChunkSource(block_id=b.block_id, start_char=a, end_char=z) for a, z in ranges],
    )


def assert_scoped(chunk, blocks, result, budget):
    by = {b.block_id: b for b in blocks}
    assert sum(len(item["text"]) for item in result) <= budget
    for item in result:
        assert item["text"] == by[item["block_id"]].content[item["start_char"]:item["end_char"]]
        assert item["text"] and item["role"] == "background_only"
        assert not any(
            s.block_id == item["block_id"]
            and max(s.start_char, item["start_char"]) < min(s.end_char, item["end_char"])
            for s in chunk.sources
        )


def test_partial_paragraph_provides_nearest_same_block_before_and_after():
    blocks = [block(0), block(1, origin="vision"), block(2)]
    b = blocks[1]
    b.content = "只有管理员能操作。该操作必须确认。失败时不能改变状态。"
    b.processing_model, b.processing_version = "fixture-vision", "v2"
    start = b.content.index("该操作")
    end = b.content.index("失败时")
    chunk = part(b, [(start, end)])
    result = neighboring_context(chunk, blocks, 1, 100)
    assert [(r["position"], r["text"]) for r in result] == [
        ("before", b.content[:start]), ("after", b.content[end:]),
    ]
    assert all(r["scope"] == "same_block" and r["origin"] == "vision" for r in result)
    assert all(r["processing_model"] == "fixture-vision" for r in result)
    assert all(r["processing_version"] == "v2" for r in result)
    assert_scoped(chunk, blocks, result, 100)
    assert neighboring_context(chunk, blocks, 2, 100) != result  # 更远相邻块仍可显式纳入。


@pytest.mark.parametrize("ranges", [[(3, 8)], [(0, 2), (7, 9)], [(2, 6), (4, 8)],
                                    [(0, 3), (3, 7)], [(0, 20)], [(3, 8), (3, 8)]])
@pytest.mark.parametrize("budget", [1, 2, 7, 100])
def test_same_block_context_never_overlaps_any_source(ranges, budget):
    b = block(1)
    chunk = part(b, ranges)
    result = neighboring_context(chunk, [b], 2, budget)
    assert_scoped(chunk, [b], result, budget)
    assert result == neighboring_context(chunk, [b], 2, budget)
    assert bool(result) == (ranges != [(0, 20)])


def test_table_header_to_data_gap_uses_real_rows_and_not_json_wrapping():
    from tc_rag.application.services.chunking import _table_rows

    b = block(1)
    b.kind = "table"
    b.content = json.dumps({"rows": [["字段", "条件"], ["旧行", "禁止提交"],
                                     ["当前行", "必须确认"], ["后行", "例外"]]}, ensure_ascii=False)
    rows = _table_rows(b.content)
    chunk = part(b, [rows[0], rows[2]])
    result = neighboring_context(chunk, [b], 1, 200)
    assert {r["text"] for r in result} == {b.content[a:z] for a, z in (rows[1], rows[3])}
    assert next(r for r in result if r["position"] == "between")["text"] == '["旧行", "禁止提交"]'
    assert_scoped(chunk, [b], result, 200)
    whole = part(b, rows)
    assert neighboring_context(whole, [b], 1, 200) == []


def test_foreign_documents_and_nontext_blocks_cannot_be_background():
    blocks = [block(0, doc="foreign"), block(1), block(2), block(3), block(4, doc="foreign")]
    blocks[1].kind, blocks[3].kind = "image", "other"
    chunk = part(blocks[2], [(0, 20)])
    assert neighboring_context(chunk, blocks, 4, 100) == []
    mixed = chunk.model_copy(deep=True)
    mixed.sources.append(ChunkSource(block_id="b0", start_char=0, end_char=1))
    assert neighboring_context(mixed, blocks, 4, 100) == []


@pytest.mark.parametrize("invalid", ["missing", "offset", "duplicate"])
def test_invalid_source_does_not_silently_preview_a_different_window(invalid):
    b = block(1)
    chunk = part(b, [(0, 20)])
    blocks = [b, b] if invalid == "duplicate" else [b]
    if invalid == "missing":
        chunk.sources[0].block_id = "missing"
    if invalid == "offset":
        chunk.sources[0].end_char = 21
    with pytest.raises(ValueError):
        neighboring_context(chunk, blocks, 1, 100)


def test_trimming_same_block_windows_preserves_the_authorized_chunk():
    b = block(1)
    chunk = part(b, [(4, 8)])
    request = {
        "chunk": chunk.model_dump(), "neighbor_context": neighboring_context(chunk, [b], 1, 8),
    }
    fit_context_budget(request, {}, 0, len)
    assert request["neighbor_context"] == []
    assert request["neighbor_context_omitted"] == [b.block_id, b.block_id]
    assert request["chunk"] == chunk.model_dump()
