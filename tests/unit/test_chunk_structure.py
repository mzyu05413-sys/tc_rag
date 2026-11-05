"""验证分块策略的原文守恒、章节限制与旧版默认输出兼容。"""

import json
from itertools import product
from pathlib import Path

import pytest
from pydantic import ValidationError

from tc_rag.application.services.chunk_stats import chunk_statistics, distribution
from tc_rag.application.services.chunking import structural_chunks
from tc_rag.core.config import Chunking, Parsing
from tc_rag.domain.models import DocumentBlock, Locator
from tc_rag.infrastructure.runtime.tokens import offline_token_counter


def block(index, text, *, kind="paragraph", path=None, **kwargs):
    return DocumentBlock(
        block_id=f"b{index}",
        document_id="d",
        kind=kind,
        order=index,
        content=text,
        heading_path=path or ["模块", "小节"],
        locator=Locator(paragraph_index=index),
        **kwargs,
    )


def assert_sources(blocks, chunks):
    """区间不能越界；精确重建 Chunk；每个应处理的字符至少覆盖一次。"""
    source = {item.block_id: item for item in blocks}
    covered = {item.block_id: set() for item in blocks}
    for chunk in chunks:
        pieces = []
        for span in chunk.sources:
            text = source[span.block_id].content
            assert 0 <= span.start_char < span.end_char <= len(text)
            pieces.append(text[span.start_char : span.end_char])
            covered[span.block_id].update(range(span.start_char, span.end_char))
        assert chunk.text == "\n".join(pieces)
    for item in blocks:
        if item.kind not in {"image", "other"} and item.content.strip():
            assert covered[item.block_id]
            if item.kind != "table":
                assert covered[item.block_id] == set(range(len(item.content)))


def legacy_case():
    value = json.loads(
        (Path(__file__).parents[1] / "fixtures/chunking_legacy.json").read_text(encoding="utf-8")
    )[0]
    return value, [DocumentBlock.model_validate(item) for item in value["blocks"]]


def test_default_exactly_matches_recorded_legacy_chunks():
    value, blocks = legacy_case()
    result = structural_chunks(blocks, value["target"], value["overlap"])
    assert [chunk.model_dump(mode="json") for chunk in result.chunks] == value["chunks"]
    assert_sources(blocks, result.chunks)


def test_default_exception_detection_keeps_legacy_case_sensitivity():
    # 不能在本轮顺手改变旧版的英文例外识别，否则默认 Chunk 指纹也会变化。
    blocks = [block(0, "x" * 80), block(1, "Except " + "y" * 25)]
    assert len(structural_chunks(blocks, 100, 0).chunks) == 2
    blocks[1] = block(1, "except " + "y" * 25)
    assert len(structural_chunks(blocks, 100, 0).chunks) == 1


@pytest.mark.parametrize(
    "attach,minimum,split", list(product([False, True], [0, 8], [False, True]))
)
@pytest.mark.parametrize("path", [["另一模块"], ["模块", "另一小节"]])
def test_exception_does_not_cross_chapters_under_any_options(attach, minimum, split, path):
    blocks = [block(0, "允许保存。"), block(1, "但是需要补充说明。", path=path)]
    result = structural_chunks(
        blocks, 14, 3, attach_headings=attach, min_tokens=minimum,
        split_long_paragraphs=split,
    )
    # 合计超出 target，正常的小节合并不能发生；错误归并例外组却会绕过预算。
    assert len(result.chunks) == 2
    assert_sources(blocks, result.chunks)


@pytest.mark.parametrize("attach,minimum", list(product([False, True], [0, 20])))
@pytest.mark.parametrize("path", [["另一模块"], ["模块", "另一小节"]])
def test_orphan_heading_cannot_attach_to_unrelated_section(attach, minimum, path):
    blocks = [block(0, "旧标题", kind="heading"), block(1, "新的正文。", path=path)]
    result = structural_chunks(blocks, 100, 0, attach_headings=attach, min_tokens=minimum)
    assert len(result.chunks) == 2
    assert_sources(blocks, result.chunks)


@pytest.mark.parametrize(
    "attach,minimum,split", list(product([False, True], [0, 20], [False, True]))
)
def test_no_grouping_or_overlap_across_documents(attach, minimum, split):
    blocks = [block(0, "旧标题", kind="heading"), block(1, "但是新文档不能保存。")]
    blocks[1] = blocks[1].model_copy(update={"document_id": "other"})
    result = structural_chunks(
        blocks, 100, 10, attach_headings=attach, min_tokens=minimum,
        split_long_paragraphs=split,
    )
    assert len(result.chunks) == 2
    assert_sources(blocks, result.chunks)


def test_heading_chain_still_attaches_to_descendant_and_is_deterministic():
    blocks = [block(0, "模块", kind="heading", path=["模块"]),
              block(1, "子节", kind="heading", path=["模块", "子节"]),
              block(2, "正文。", path=["模块", "子节"])]
    first = structural_chunks(blocks, 100, 0, attach_headings=True)
    assert len(first.chunks) == 1
    assert first.chunks == structural_chunks(blocks, 100, 0, attach_headings=True).chunks
    assert_sources(blocks, first.chunks)


def test_heading_attachment_and_small_sibling_merge_are_lossless_and_stable():
    _, blocks = legacy_case()
    attached = structural_chunks(blocks, 900, 100, attach_headings=True)
    assert len(attached.chunks) == 8
    merged = structural_chunks(blocks, 900, 100, attach_headings=True, min_tokens=200)
    assert len(merged.chunks) < len(attached.chunks)
    for result in (attached, merged):
        assert_sources(blocks, result.chunks)
        by_id = {item.block_id: item for item in blocks}
        assert all(
            len({by_id[s.block_id].heading_path[0] for s in c.sources}) == 1 for c in result.chunks
        )
    again = structural_chunks(blocks, 900, 100, attach_headings=True, min_tokens=200)
    assert merged.chunks == again.chunks


def test_heading_plus_large_table_splits_by_rows_and_keeps_prefix():
    table = json.dumps(
        {
            "schema_version": "1.0",
            "rows": [["Field", "Value"], *[[f"row{i}", "x" * 20] for i in range(8)]],
        }
    )
    blocks = [block(0, "Title", kind="heading"), block(1, table, kind="table")]
    result = structural_chunks(blocks, 90, 0, attach_headings=True)
    assert len(result.chunks) > 1
    assert result.chunks[0].sources[0].block_id == "b0"
    assert all("Field" in c.text for c in result.chunks)
    assert all(any(f"row{i}" in c.text for c in result.chunks) for i in range(8))
    assert_sources(blocks, result.chunks)


@pytest.mark.parametrize("overlap", [0, 15])
def test_sentence_split_preserves_exact_offsets_exceptions_and_vision_origin(overlap):
    raw = "首句允许保存。" + "必须保留的数据。" * 4 + "但是名称为空不能保存。\n金额为3.14元。末句。"
    blocks = [block(0, raw, origin="vision", processing_model="fixture")]
    result = structural_chunks(blocks, 24, overlap, split_long_paragraphs=True)
    assert len(result.chunks) > 1
    assert_sources(blocks, result.chunks)
    assert any("必须保留的数据。但是名称为空不能保存。" in c.text for c in result.chunks)
    assert any("3.14" in c.text for c in result.chunks)
    assert blocks[0].content == raw and blocks[0].origin == "vision"
    assert any("按句拆分" in issue.description for issue in result.issues)


def test_unsplittable_sentence_is_whole_with_warning_and_trailing_heading_is_kept():
    blocks = [block(0, "长" * 120), block(1, "末尾标题", kind="heading", path=["末节"])]
    result = structural_chunks(blocks, 30, 0, split_long_paragraphs=True, attach_headings=True)
    assert result.chunks[0].text == "长" * 120
    assert any(result.chunks[0].chunk_id in issue.object_ids for issue in result.issues)
    assert_sources(blocks, result.chunks)


def test_isolated_and_image_blocks_are_preserved_but_not_chunked():
    blocks = [
        block(0, "正文。"),
        block(1, "页眉", kind="other"),
        block(2, "图片资产", kind="image"),
    ]
    saved = [b.model_dump() for b in blocks]
    result = structural_chunks(blocks, 100, 0)
    assert result.chunks[0].text == "正文。"
    assert [b.model_dump() for b in blocks] == saved
    assert_sources(blocks, result.chunks)


@pytest.mark.parametrize("minimum", [-1, 900, 901])
def test_minimum_must_be_nonnegative_and_below_target(minimum):
    with pytest.raises(ValidationError):
        Chunking(min_tokens=minimum)


def test_default_new_fields_are_omitted_from_legacy_snapshots():
    assert "attach_headings" not in Chunking().model_dump()
    assert "min_tokens" not in Chunking().model_dump()
    assert "split_long_paragraphs" not in Chunking().model_dump()
    assert "docx_extended" not in Parsing().model_dump()
    assert "pdf_running_text" not in Parsing().model_dump()
    assert Chunking(attach_headings=True).model_dump()["attach_headings"] is True
    assert Parsing(pdf_running_text="isolate").model_dump()["pdf_running_text"] == "isolate"


def test_diagnostic_empty_distribution_and_heading_count():
    assert distribution([]) == {"count": 0, "min": None, "median": None, "p90": None, "max": None}
    blocks = [block(0, "标题", kind="heading")]
    chunks = structural_chunks(blocks, 100, 0).chunks
    stats = chunk_statistics(blocks, chunks, [], 100, None)
    assert stats["heading_only_chunks"] == 1
    assert stats["small_chunks"]["100"]["count"] == 1
    assert stats["visual_tokens"] is None


def test_missing_encoding_cache_cannot_trigger_network(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "tc_rag.infrastructure.runtime.tokens.token_counter",
        lambda _: pytest.fail("不得加载会联网的计数器"),
    )
    with pytest.raises(ValueError, match="缓存缺失"):
        offline_token_counter("tiktoken_approx")
    assert offline_token_counter("character_approx")("abc") == 3
