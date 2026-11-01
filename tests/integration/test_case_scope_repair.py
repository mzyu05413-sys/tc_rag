"""错误测试点 ID 应进入现有模型修复，不能与检索度量耦合或污染缓存。"""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from tc_rag.application.requests import CasesRequest
from tc_rag.bootstrap import build_pipeline
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.providers.openai_sdk import ProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["cosine", "euclidean", "dot"])
@pytest.mark.parametrize("response_format", ["json_schema", "json_object"])
@pytest.mark.parametrize("fixed", [True, False])
async def test_foreign_case_point_id_uses_bounded_safe_repair(
    tmp_path, metric, response_format, fixed
):
    from test_pipeline import FixtureLLM, point_topics, setup
    from test_provider import completion, provider

    original, document, _, _ = setup(tmp_path)
    config = original.config.model_copy(deep=True)
    config.retrieval.metric = metric
    config.retrieval.normalize = True
    run_id = "run_scoped_cases"
    pipeline = build_pipeline(config, run_id, FixtureLLM(), FakeEmbedder())
    await pipeline.points(document, "生成边界用例", tmp_path / "out", run_id=run_id)
    calls, contracts, records, events = [], [], [], []

    def handler(request):
        payload = json.loads(request.content)
        body = json.loads(payload["messages"][1]["content"])
        calls.append(body)
        contracts.append(
            body["output_schema"]
            if response_format == "json_object"
            else payload["response_format"]["json_schema"]["schema"]
        )
        point = body["points"][0]
        rule = next(r for r in body["rules"] if r["rule_id"] == point["rule_ids"][0])
        invalid = len(calls) == 1 or not fixed
        case = {
            "case_id": "fixture_case",
            "point_ids": ["private-generated-foreign-point" if invalid else point["point_id"]],
            "title": "private-generated-case-text" if invalid else "边界用例",
            "steps": [
                {
                    "action": "提交边界数量",
                    "expected": "最多300个，数量为整数。",
                    "rule_ids": [rule["rule_id"]],
                    "evidence_ids": rule["evidence_ids"],
                }
            ],
        }
        return httpx.Response(200, json=completion(json.dumps({"cases": [case]})))

    adapter = provider(
        handler, records, attempts=1, repairs=1, response_format=response_format
    )
    adapter.progress = events.append
    pipeline.llm = adapter
    request = CasesRequest(
        run_id=run_id, instruction="生成边界用例", output_dir=tmp_path / "out"
    )
    try:
        if fixed:
            result = await pipeline.cases(request)
            assert result["cases"] == 1
            assert Path(result["excel"]).is_file()
            assert len(point_topics(Path(result["xmind"]))) == 1
            assert records[-1].error is None
        else:
            # 同一修复预算耗尽就停止，不猜测 ID，也不丢弃错误用例伪装成功。
            with pytest.raises(ProviderError, match="JSON/Schema") as error:
                await pipeline.cases(request)
            assert "private-generated-" not in str(error.value)
            assert pipeline.read(run_id, "failure")["stage"] == "generate"
            assert not list((tmp_path / "out").rglob("*.xlsx"))
    finally:
        await adapter.close()

    assert len(calls) == len(records) == 2
    assert records[0].error == "SchemaOutputError"
    assert sum(event.kind == "repair" for event in events) == 1
    allowed = {point["point_id"] for point in calls[0]["points"]}
    for contract in contracts:
        assert set(contract["$defs"]["BatchPointId"]["enum"]) == allowed
    details = calls[1]["validation_errors"]
    assert any(
        detail["path"] == ["cases", 0, "point_ids", 0]
        and detail["error"] == "enum"
        and set(detail["allowed_values"]) == allowed
        for detail in details
    )
    assert "private-generated-" not in json.dumps(calls[1]) + str(records)
    with sqlite3.connect(config.storage.database) as connection:
        cache = connection.execute(
            "SELECT payload FROM stage_snapshots WHERE json_extract(payload, '$.stage')=?",
            ("model_cache",),
        ).fetchall()
    assert all("private-generated-" not in row[0] for row in cache)
