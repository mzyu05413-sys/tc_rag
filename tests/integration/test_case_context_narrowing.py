"""用例上下文收窄（A5）：核心证据只取测试点引用规则的证据，不装同功能全量。"""

from copy import deepcopy

import pytest
from test_pipeline import setup

from tc_rag.domain import models as m
from tc_rag.domain.pipeline import Extraction


def _extraction_with_two_rules():
    feature = m.Feature(
        feature_id="f", name="订单管理", description="", evidence_ids=["e1", "e2"],
    )
    r1 = m.RequirementRule(
        rule_id="r1", feature_id="f", kind="permission",
        statement="仅管理员可删除订单", evidence_ids=["e1"],
    )
    r2 = m.RequirementRule(
        rule_id="r2", feature_id="f", kind="state_transition",
        statement="订单可从待处理流转到已确认", evidence_ids=["e2"],
    )
    e1 = m.EvidenceSpan(
        evidence_id="e1", block_id="b", locator=m.Locator(page=1), quote="管理员删除",
    )
    e2 = m.EvidenceSpan(
        evidence_id="e2", block_id="b", locator=m.Locator(page=1), quote="待处理流转",
    )
    return Extraction(features=[feature], rules=[r1, r2], evidence=[e1, e2])


@pytest.mark.asyncio
async def test_generate_batch_narrows_rules_and_evidence_to_referenced(tmp_path, monkeypatch):
    pipeline, _, _, model = setup(tmp_path)
    pipeline.active_run_id = "run_demo"
    pipeline.repository.save("runs", "run_demo", m.Run(
        run_id="run_demo", config_snapshot={}, status="running",
    ))
    extracted = _extraction_with_two_rules()
    point = m.TestPoint(
        point_id="p", rule_ids=["r1"], technique="permission",
        objective="验证管理员删除权限",
    )
    seen = []
    original = model.generate

    async def observe(request, schema):
        seen.append(deepcopy(request))
        return await original(request, schema)

    model.generate = observe

    async def no_supplement(request, direct_ids, query, schema):
        request["supplemental_evidence"] = []

    monkeypatch.setattr(pipeline, "_supplement", no_supplement)
    await pipeline._generate_batch("生成边界用例", [point], extracted)

    assert [r["rule_id"] for r in seen[0]["rules"]] == ["r1"]
    assert [e["evidence_id"] for e in seen[0]["evidence"]] == ["e1"]
