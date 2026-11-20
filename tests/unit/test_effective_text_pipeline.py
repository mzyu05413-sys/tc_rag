"""有效文本视图接入索引（A9）：来源切片去掉作废区间，整段作废则不入索引。"""

from tc_rag.application.orchestration.pipeline import Pipeline
from tc_rag.domain import models as m


def test_retrieval_evidence_strips_deprecated_source_slice():
    block = m.DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content="abcdef", locator=m.Locator(page=1),
        deprecated_ranges=[m.DeprecatedRange(start_char=2, end_char=4)],
    )
    chunk = m.Chunk(
        chunk_id="c", text="abcdef", token_estimate=1, fingerprint="fp",
        sources=[m.ChunkSource(block_id="b", start_char=0, end_char=6)],
    )
    evidence, dropped = Pipeline._retrieval_evidence([], [block], [chunk])
    assert [e.quote for e in evidence] == ["abef"]
    assert dropped == []


def test_retrieval_evidence_skips_fully_deprecated_source_slice():
    block = m.DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content="abcdef", locator=m.Locator(page=1),
        deprecated_ranges=[m.DeprecatedRange(start_char=0, end_char=6)],
    )
    chunk = m.Chunk(
        chunk_id="c", text="abcdef", token_estimate=1, fingerprint="fp",
        sources=[m.ChunkSource(block_id="b", start_char=0, end_char=6)],
    )
    evidence, dropped = Pipeline._retrieval_evidence([], [block], [chunk])
    assert evidence == []
    assert dropped == []


def test_retrieval_evidence_drops_over_budget_source_slice():
    # 超过 embedding 单条字节预算的原文切片不入索引，否则整次 prepare 失败；
    # 模型证据（rule 直接引用）不受影响，仍原样保留。
    block = m.DocumentBlock(
        block_id="b", document_id="d", kind="paragraph", order=0,
        content="甲" * 3000, locator=m.Locator(page=1),
    )
    chunk = m.Chunk(
        chunk_id="c", text=block.content, token_estimate=1, fingerprint="fp",
        sources=[m.ChunkSource(block_id="b", start_char=0, end_char=3000)],
    )
    evidence, dropped = Pipeline._retrieval_evidence([], [block], [chunk], max_input_bytes=1000)
    assert evidence == []
    assert len(dropped) == 1
    assert dropped[0].startswith("source_")
    # 不设预算时保持原行为，仍然入索引。
    evidence, dropped = Pipeline._retrieval_evidence([], [block], [chunk])
    assert len(evidence) == 1
    assert dropped == []
