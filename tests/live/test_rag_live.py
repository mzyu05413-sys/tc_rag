"""真实模型烟测必须由使用者显式启用；默认跳过，限制实际请求次数。"""

import os
from pathlib import Path

import pytest
from docx import Document

from tc_rag.bootstrap import build_pipeline
from tc_rag.core.config import load_config
from tc_rag.interfaces.workflow import _close


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_TC_RAG_LIVE") != "1", reason="需显式设置 RUN_TC_RAG_LIVE=1")
async def test_real_model_small_rag_points(tmp_path):
    """只使用虚构的一条需求；不自动批准测试点，不代表业务质量验收。"""
    local = Path("workdir/local.yaml")
    config = load_config(
        local if local.exists() else Path("config/defaults.yaml"),
        cli_overrides={
            "project": {"workdir": str(tmp_path / "work")},
            "storage": {"database": str(tmp_path / "live.sqlite")},
            "chunking": {"token_counter": "character_approx"},
            "runtime": {"budget": {"max_calls": 8, "max_cost": None}},
        },
    )
    if not config.models.llm.model or not os.getenv(config.models.llm.api_key_env):
        pytest.fail("启用 live 后必须明确提供模型与密钥环境变量")
    doc = Document()
    doc.add_paragraph("批量操作每次最多选择300个对象，对象数量为整数；超过300个时拒绝执行。")
    source = tmp_path / "fiction.docx"
    doc.save(source)
    pipeline = build_pipeline(config, "run_live_smoke")
    try:
        await pipeline.points(
            source, "生成测试点，不编造界面文案", tmp_path / "out", run_id="run_live_smoke"
        )
        assert pipeline.read("run_live_smoke", "extract")["rules"]
        assert pipeline.read("run_live_smoke", "retrieval")["bundles"]
        assert pipeline.read("run_live_smoke", "plan_points")["points"]
        assert pipeline.manifest("run_live_smoke")["metrics"]["actual_model_calls"] <= 8
    finally:
        await _close(pipeline)
