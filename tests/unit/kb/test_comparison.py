"""配对统计保留未知，并拒绝不同金标或统计协议。"""

from copy import deepcopy

import pytest

from tc_rag.application.kb.compare import compare_reports


def test_paired_differences_and_protocol_guard():
    base = {"gold_semantic_fingerprint": "g", "protocol": {"version": "v1"},
            "mode": "retrieval", "rows": [
                {"query_id": "q1", "query_family": "a", "k": 5, "recall": 0.2},
                {"query_id": "q2", "query_family": "b", "k": 5, "recall": None},
            ]}
    other = deepcopy(base)
    other["rows"][0]["recall"] = 0.8
    metrics = compare_reports(base, other)["metrics"]["recall"]
    assert metrics["mean"] == pytest.approx(0.6)
    assert metrics["n"] == 1
    assert metrics["ci95"] is None
    other["gold_semantic_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="相同金标"):
        compare_reports(base, other)
