"""真实HTTP适配器的同分策略，使用MockTransport，不访问模型。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from tc_rag.application.ports.contracts import RankedCandidate
from tc_rag.core.config import Endpoint, Retrieval, Runtime, load_config
from tc_rag.infrastructure.providers.rerank import HttpReranker


@pytest.mark.asyncio
@pytest.mark.parametrize("policy, expected", [
    (None, ["d", "c", "b", "a"]),
    ("provider_order", ["d", "c", "b", "a"]),
    ("input_order", ["d", "b", "c", "a"]),
])
async def test_exact_tie_and_distinct_score_mapping(policy, expected):
    requests, records = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [
            {"index": 3, "relevance_score": 1.0000001},
            {"index": 2, "relevance_score": 1.0},
            {"index": 1, "relevance_score": 1.0},
            {"index": 0, "relevance_score": 0.9},
        ]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    kwargs = {} if policy is None else {"tie_policy": policy}
    adapter = HttpReranker(
        Endpoint(adapter="http", model="fixture", base_url="https://fixture/rerank"),
        Runtime(), "run", records.append, client, **kwargs,
    )
    candidates = [RankedCandidate(key, "text_" + key) for key in "abcd"]
    try:
        ranked = await adapter.rank("question", candidates)
        assert [r.object_id for r in ranked] == expected
        assert {r.object_id: r.text for r in ranked} == {c.object_id: c.text for c in candidates}
        assert [r.score for r in ranked] == [1.0000001, 1.0, 1.0, 0.9]
        assert requests[0]["documents"] == [c.text for c in candidates]
        assert records[0].error is None
    finally:
        await adapter.close()


def test_config_default_snapshot_compatibility_and_invalid_policy():
    config = Retrieval()
    assert config.rerank_tie_policy == "provider_order"
    assert "rerank_tie_policy" not in config.model_dump()
    assert Retrieval(rerank_tie_policy="input_order").model_dump()[
        "rerank_tie_policy"] == "input_order"
    with pytest.raises(ValidationError):
        Retrieval.model_validate({"rerank_tie_policy": "unknown"})


def test_yaml_and_environment_policy_precedence(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("retrieval:\n  rerank_tie_policy: input_order\n", "utf-8")
    assert load_config(path).retrieval.rerank_tie_policy == "input_order"
    monkeypatch.setenv("TC_RAG_RETRIEVAL__RERANK_TIE_POLICY", "provider_order")
    assert load_config(path).retrieval.rerank_tie_policy == "provider_order"
