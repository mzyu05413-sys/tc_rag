"""来源去重（A10）：按 (block_id, quote) 精确折叠，记录覆盖关系。"""

from tc_rag.core.config import Retrieval
from tc_rag.infrastructure.retrieval.dedupe import dedupe_sources


def _evidence(evidence_id, block_id, quote):
    return {"evidence_id": evidence_id, "block_id": block_id, "quote": quote}


def test_dedupe_exact_same_source_keeps_first_and_records_coverage():
    a = _evidence("a", "b1", "最多300个")
    b = _evidence("b", "b1", "最多300个")
    c = _evidence("c", "b1", "数量为整数")
    result, coverage = dedupe_sources([a, b, c])
    assert [e["evidence_id"] for e in result] == ["a", "c"]
    assert coverage == {"a": ["b"]}


def test_dedupe_keeps_different_blocks_with_same_quote():
    a = _evidence("a", "b1", "相同文字")
    b = _evidence("b", "b2", "相同文字")
    result, coverage = dedupe_sources([a, b])
    assert [e["evidence_id"] for e in result] == ["a", "b"]
    assert coverage == {}


def test_dedupe_empty_input():
    assert dedupe_sources([]) == ([], {})


def test_retrieval_config_dedupe_defaults_off_and_hidden_from_snapshot():
    assert Retrieval().dedupe_sources is False
    assert "dedupe_sources" not in Retrieval().model_dump(mode="json")
    assert Retrieval(dedupe_sources=True).model_dump(mode="json")["dedupe_sources"] is True
