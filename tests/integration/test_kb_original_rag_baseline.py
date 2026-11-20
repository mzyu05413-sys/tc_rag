"""固定虚构产物摘要，剔除 DOCX 打包时间和随机业务 ID。"""

import hashlib
import json
from pathlib import Path

import pytest
from test_pipeline import setup


@pytest.mark.asyncio
async def test_original_rag_semantic_baseline(tmp_path):
    baseline = json.loads((Path(__file__).parents[1] / "fixtures" /
                           "kb_original_rag_baseline.json").read_text("utf-8"))
    pipeline, document, _, _ = setup(tmp_path)
    result = await pipeline.generate(
        document, "生成边界用例", tmp_path / "out", run_id="run_demo",
    )
    chunk = pipeline.read("run_demo", "chunk")
    bundle = pipeline.read("run_demo", "retrieval")["bundles"][0]
    cases = pipeline.read("run_demo", "cases")
    case = cases["cases"][0]
    matrix = pipeline.retriever.index.vectors
    observed = {
        "block_text": chunk["blocks"][0]["content"],
        "chunk_ranges": [[s["start_char"], s["end_char"]]
                         for c in chunk["chunks"] for s in c["sources"]],
        "vector_sha256_float32_le": hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest(),
        "vector_shape": list(matrix.shape),
        "first_hit_is_direct": bundle["hits"][0]["object_id"] in bundle["direct_ids"],
        "hit_ranks": [hit["rank"] for hit in bundle["hits"]],
        "case_title": case["title"], "action": case["steps"][0]["action"],
        "expected": case["steps"][0]["expected"], "priority": case["priority"],
        "status": case["status"],
    }
    assert observed == baseline
    assert set(case["steps"][0]["rule_ids"]) <= {r["rule_id"] for r in cases["rules"]}
    assert set(case["steps"][0]["evidence_ids"]) <= {e["evidence_id"] for e in cases["evidence"]}
    assert Path(result["excel"]).is_file() and Path(result["xmind"]).is_file()
