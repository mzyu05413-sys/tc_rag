import json
import runpy
from copy import deepcopy

import pytest
from test_pipeline import setup

from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Run
from tc_rag.domain.pipeline import PointBatch, StageSnapshot


def records(pipeline):
    ids = pipeline.read("run_demo", "generation_contexts")["record_ids"]
    return [pipeline.repository.get("stage_snapshots", key, StageSnapshot).payload for key in ids]


@pytest.mark.asyncio
async def test_both_generation_stages_capture_actual_evidence_without_changing_requests(tmp_path):
    pipeline, document, _, model = setup(tmp_path)
    original = model.generate
    seen = []

    async def observe(request, schema):
        seen.append(deepcopy(request))
        return await original(request, schema)

    model.generate = observe
    await pipeline.generate(document, "生成边界用例", tmp_path / "out", run_id="run_demo")
    contexts = records(pipeline)
    assert {r["stage"] for r in contexts} == {"plan_points", "generate"}
    for row in contexts:
        request = next(r for r in seen if fingerprint(r) == row["request_fingerprint"])
        assert row["core_evidence"] == request["evidence"]
        assert row["retrieved_evidence"] == request["supplemental_evidence"]
        assert row["state"] == "model_completed" and row["provider_attempted"]
        assert row["document_fingerprint"] == pipeline.read("run_demo", "input")[
            "document_fingerprint"]
    export = runpy.run_path("eval/kb/export_generation_contexts.py")["export"]
    export(tmp_path / "data.sqlite", "run_demo", tmp_path / "context_export")
    exported = json.loads((tmp_path / "context_export/contexts.json").read_text("utf-8"))
    assert [r["payload"] for r in exported["records"]] == contexts


@pytest.mark.asyncio
async def test_cache_and_model_error_are_not_confused_with_success(tmp_path):
    pipeline, _, _, model = setup(tmp_path)
    pipeline.active_run_id = "run_demo"
    pipeline.repository.save("runs", "run_demo", Run(
        run_id="run_demo", config_snapshot={}, status="running",
    ))
    request = {"stage": "plan_points", "rules": [], "evidence": [],
               "supplemental_evidence": [], "unused_secret": "must-not-be-exported"}
    await pipeline._model(request, PointBatch)
    await pipeline._model(request, PointBatch)
    assert model.calls == 1
    assert [r["state"] for r in records(pipeline)] == ["model_completed", "cache_hit"]
    assert records(pipeline)[1]["provider_attempted"] is False
    pipeline.config.runtime.cache_enabled = False

    async def fail(request, schema):
        raise RuntimeError("private-supplier-body")

    model.generate = fail
    with pytest.raises(RuntimeError):
        await pipeline._model(request, PointBatch)
    row = records(pipeline)[-1]
    assert row["state"] == "model_error" and row["error_type"] == "RuntimeError"
    assert "private-supplier-body" not in json.dumps(row)
    assert "must-not-be-exported" not in json.dumps(row)


@pytest.mark.asyncio
async def test_observation_uses_budget_trimmed_supplements(tmp_path, monkeypatch):
    pipeline, document, _, model = setup(tmp_path)
    await pipeline.points(document, "生成测试点", tmp_path / "out", run_id="run_demo")
    bundle = deepcopy(pipeline.read("run_demo", "retrieval")["bundles"][0])
    evidence = pipeline.read("run_demo", "extract")["evidence"][0]
    bundle["supplemental_evidence"] = [dict(evidence, evidence_id=key) for key in ["keep", "drop"]]

    async def retrieve(query, direct_ids):
        return deepcopy(bundle)

    def budget(request):
        if len(request.get("supplemental_evidence", [])) > 1:
            raise ValueError("fixture budget")

    monkeypatch.setattr(pipeline.retriever, "retrieve", retrieve)
    monkeypatch.setattr(pipeline, "_check_budget", budget)
    request = {"stage": "plan_points", "rules": [], "evidence": [evidence]}
    await pipeline._supplement(request, {evidence["evidence_id"]}, "query", PointBatch)
    await pipeline._model(request, PointBatch)
    observed = records(pipeline)[-1]
    assert [e["evidence_id"] for e in observed["retrieved_evidence"]] == ["keep"]
    assert observed["core_evidence"] == [evidence]
    saved = pipeline.read("run_demo", "retrieval")["bundles"][-1]
    assert saved["omitted_ids"] == ["drop"]
    assert saved["generation"]["evidence_fingerprint"] == observed["evidence_fingerprint"]
