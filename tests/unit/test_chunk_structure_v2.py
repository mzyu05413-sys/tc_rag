"""新策略验证完整尾句、原文守恒和表头来源，不以块数少作为正确性依据。"""

import json

import pytest

from tc_rag.application.services.chunking import _table_rows, structural_chunks
from tc_rag.domain.models import DocumentBlock


def block(i, text, *, kind="paragraph", path=None):
    return DocumentBlock(
        block_id=f"b{i}", document_id="doc", order=i, kind=kind, content=text,
        heading_path=["模块", "小节"] if path is None else path, locator={},
    )


def assert_lossless(blocks, result):
    by = {b.block_id: b for b in blocks}
    covered = {b.block_id: set() for b in blocks}
    for c in result.chunks:
        assert c.text == "\n".join(
            by[s.block_id].content[s.start_char:s.end_char] for s in c.sources
        )
        for s in c.sources:
            assert 0 <= s.start_char < s.end_char <= len(by[s.block_id].content)
            covered[s.block_id].update(range(s.start_char, s.end_char))
    for b in blocks:
        if b.kind == "table":
            assert all(set(range(a, z)) <= covered[b.block_id] for a, z in _table_rows(b.content))
        else:
            assert covered[b.block_id] == set(range(len(b.content)))


def test_large_paragraph_can_share_complete_tail_sentence_with_exact_offsets():
    blocks = [block(0, "甲" * 45 + "。尾句。"), block(1, "乙" * 45 + "。")]
    old = structural_chunks(blocks, 70, 10)
    new = structural_chunks(blocks, 70, 10, strategy="structural_v2")
    assert len(new.chunks) == 2
    assert all(s.block_id != "b0" for s in old.chunks[1].sources)
    source = new.chunks[1].sources[0]
    assert source.block_id == "b0" and source.start_char == 46
    assert new.chunks[1].text.startswith("尾句。\n")
    assert all(c.token_estimate <= 70 for c in new.chunks)
    assert_lossless(blocks, new)
    assert new.chunks == structural_chunks(blocks, 70, 10, strategy="structural_v2").chunks


@pytest.mark.parametrize("overlap,target,next_size", [(0, 70, 45), (10, 70, 69)])
def test_overlap_is_not_forced_when_disabled_or_next_unit_has_no_room(overlap, target, next_size):
    blocks = [block(0, "甲" * 45 + "。尾句。"), block(1, "乙" * next_size)]
    result = structural_chunks(blocks, target, overlap, strategy="structural_v2")
    assert all(s.block_id != "b0" for s in result.chunks[1].sources)
    assert_lossless(blocks, result)


def test_exception_is_not_shared_without_its_condition():
    blocks = [block(0, "甲" * 30 + "。允许提交。"), block(1, "但是失败时不能保存。"),
              block(2, "乙" * 35 + "。")]
    result = structural_chunks(blocks, 65, 12, strategy="structural_v2")
    assert all(s.block_id not in {"b0", "b1"} for s in result.chunks[-1].sources)
    assert_lossless(blocks, result)


def test_long_paragraph_sentence_split_preserves_origin_and_all_characters():
    b = block(0, "金额为3.14元。" + "每次提交必须保存。" * 18 + "但是失败不能保存。")
    b.origin, b.processing_model = "vision", "fixture"
    result = structural_chunks(
        [b], 60, 18, strategy="structural_v2", split_long_paragraphs=True
    )
    assert len(result.chunks) > 2 and all(c.token_estimate <= 60 for c in result.chunks)
    assert any("3.14" in c.text for c in result.chunks)
    assert any("每次提交必须保存。但是失败不能保存。" in c.text for c in result.chunks)
    assert_lossless([b], result)
    assert b.origin == "vision" and b.processing_model == "fixture"


@pytest.mark.parametrize("declared", [False, True])
def test_table_rows_are_pre_grouped_without_inventing_header(declared):
    rows = [["字段", "限制"], *[[f"行{i}", "值" * 20] for i in range(7)]]
    data = {"rows": rows}
    if declared:
        data.update(header_rows=[0], header_source="docx_repeat_header")
    table = block(1, json.dumps(data, ensure_ascii=False), kind="table")
    blocks = [block(0, "前置说明。"), table, block(2, "但是例外必须保留。")]
    result = structural_chunks(blocks, 65, 0, strategy="structural_v2")
    assert_lossless(blocks, result)
    spans = _table_rows(table.content)
    counts = [sum(s.block_id == table.block_id and s.start_char <= a and s.end_char >= z
                  for c in result.chunks for s in c.sources) for a, z in spans]
    assert all(n == 1 for n in counts[1:])
    assert counts[0] > 1 if declared else counts[0] == 1
    assert any("表头未明确标记" in i.description for i in result.issues) is not declared


def test_long_indivisible_row_has_warning_instead_of_truncation():
    b = block(0, json.dumps({"rows": [["字段", "值" * 200]]}), kind="table")
    result = structural_chunks([b], 30, 0, strategy="structural_v2")
    assert_lossless([b], result)
    assert len(result.chunks) == 1 and result.chunks[0].token_estimate > 30
    assert any(result.chunks[0].chunk_id in i.object_ids for i in result.issues)


def test_v2_does_not_inject_heuristic_cross_page_header():
    a = block(0, json.dumps({"rows": [["普通数据", "1"], ["另一行", "2"]]}), kind="table")
    b = block(1, json.dumps({"rows": [["第三行", "3"]]}), kind="table")
    a.locator.page, b.locator.page = 1, 2
    result = structural_chunks([a, b], 80, 0, strategy="structural_v2")
    assert all(s.block_id != a.block_id for c in result.chunks if any(
        s.block_id == b.block_id for s in c.sources
    ) for s in c.sources)
    assert any("疑似续表" in i.description for i in result.issues)
    assert_lossless([a, b], result)


@pytest.mark.parametrize("different_document", [False, True])
def test_v2_never_overlaps_across_chapters_or_documents(different_document):
    a, b = block(0, "第一条。尾句。"), block(1, "第二条。")
    if different_document:
        b.document_id = "other_doc"
    else:
        b.heading_path = ["另一章", "小节"]
    result = structural_chunks([a, b], 100, 30, strategy="structural_v2", attach_headings=True)
    assert len(result.chunks) == 2
    assert [s.block_id for s in result.chunks[1].sources] == [b.block_id]
    assert_lossless([a, b], result)


@pytest.mark.parametrize("headers", [[True], [1], [0, 2], [0, 1, 2, 3, 4], "0"])
def test_v2_rejects_ambiguous_header_metadata_without_dropping_rows(headers):
    data = {
        "rows": [["数据" * 8], ["记录" * 8], ["条目" * 8]],
        "header_rows": headers, "header_source": "docx_repeat_header",
    }
    b = block(0, json.dumps(data, ensure_ascii=False), kind="table")
    result = structural_chunks([b], 30, 0, strategy="structural_v2")
    assert_lossless([b], result)
    assert any("表头未明确标记" in i.description for i in result.issues)
    spans = _table_rows(b.content)
    assert all(sum(s.start_char <= start and s.end_char >= end
                   for c in result.chunks for s in c.sources) == 1 for start, end in spans)


@pytest.mark.parametrize(
    "attach,split", [(False, False), (True, False), (False, True), (True, True)],
)
def test_v2_mixed_structures_preserve_ranges_and_determinism(attach, split):
    import random

    randomizer = random.Random(20261104)
    blocks = []
    for i in range(30):
        path = [f"章{i // 10}", f"节{i // 5}"]
        text = "条件允许时保存。" * randomizer.randint(1, 10)
        kind = "paragraph"
        if i % 10 == 0:
            kind, text = "heading", path[-1]
        elif i % 7 == 0:
            kind, text = "table", json.dumps({"rows": [[f"行{n}", "值" * 15] for n in range(6)]})
        elif i % 3 == 0:
            text = "但是失败时不得保存。"
        blocks.append(block(i, text, kind=kind, path=path))
    kwargs = dict(strategy="structural_v2", attach_headings=attach, split_long_paragraphs=split)
    result = structural_chunks(blocks, 70, 20, **kwargs)
    assert_lossless(blocks, result)
    assert result.chunks == structural_chunks(blocks, 70, 20, **kwargs).chunks
    by = {b.block_id: b for b in blocks}
    for c in result.chunks:
        assert len({by[s.block_id].heading_path[0] for s in c.sources}) == 1
        if c.token_estimate > 70:
            assert any(c.chunk_id in i.object_ids for i in result.issues)
