"""遗漏规则缺口核查接线（A3）：gap_check_rounds > 0 时按指令检索并报告未引用证据。"""

import pytest
from test_pipeline import setup


@pytest.mark.asyncio
async def test_gap_check_reports_unreferenced_evidence_when_enabled(tmp_path):
    pipeline, document, _, _ = setup(tmp_path)
    pipeline.config.planning.gap_check_rounds = 1
    await pipeline.points(document, "生成边界用例", tmp_path / "out", run_id="run_demo")
    kinds = {item["kind"] for item in pipeline.read("run_demo", "issues")["items"]}
    # 来源切片与提炼证据同文，但未被规则引用，属于缺口候选报告。
    assert "gap" in kinds
