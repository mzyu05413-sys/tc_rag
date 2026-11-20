import asyncio
import json
import runpy

import pytest

from tc_rag.application.ports.contracts import RankedCandidate


def test_holdout_requires_explicit_opt_in(tmp_path):
    execute = runpy.run_path("eval/kb/rerank_fixed_pool.py")["execute"]
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"questions": [{"split": "test"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="集合不匹配"):
        asyncio.run(execute(pool, tmp_path / "out", tmp_path / "missing", tmp_path))
    with pytest.raises(ValueError, match="缺少留出"):
        asyncio.run(execute(pool, tmp_path / "out", tmp_path / "missing", tmp_path,
                            allow_holdout=True))


def test_fixed_pool_reordering_and_invalid_set():
    reorder = runpy.run_path("eval/kb/rerank_fixed_pool.py")["reorder"]
    pool = {"query_id": "q", "question": "question", "candidates": [
        {"chunk_id": "a", "text": "A"}, {"chunk_id": "b", "text": "B"}]}

    class Fake:
        async def rank(self, query, candidates):
            return [RankedCandidate("b", "B", .9), RankedCandidate("a", "A", .1)]

    row = asyncio.run(reorder(pool, Fake()))
    assert row["before"] == ["a", "b"] and row["after"] == ["b", "a"]
    pool["candidates"] = [{"chunk_id": "a", "text": "A"}]
    with pytest.raises(ValueError, match="改变候选"):
        asyncio.run(reorder(pool, Fake()))


def test_policy_override_preview_does_not_edit_config_or_create_results(tmp_path, capsys):
    execute = runpy.run_path("eval/kb/rerank_fixed_pool.py")["execute"]
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"questions": [{"split": "dev", "question": "q",
        "candidates": [{"chunk_id": "a", "text": "A"}]}]}), encoding="utf-8")
    config = tmp_path / "config.yaml"
    original = "retrieval:\n  rerank_tie_policy: provider_order\n"
    config.write_text(original, encoding="utf-8")
    asyncio.run(execute(pool, tmp_path / "out", config, tmp_path, tie_policy="input_order"))
    assert "同分策略：input_order" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()
    assert config.read_text("utf-8") == original
