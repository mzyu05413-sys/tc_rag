"""同一原段多子块的提炼回放：背景进入请求，但不能进入有效引用闭包。"""

from pathlib import Path

import pytest
from docx import Document

from tc_rag.application.ports.contracts import Capabilities, ModelResult
from tc_rag.bootstrap import build_pipeline
from tc_rag.core.config import AppConfig
from tc_rag.infrastructure.providers.fakes import FakeEmbedder


class ScopedLLM:
    capabilities = Capabilities(structured_output=True)

    def __init__(self):
        self.requests = []

    async def generate(self, request, output_schema):
        if request["stage"] != "extract":
            return ModelResult(output=output_schema.model_validate({}))
        self.requests.append(request)
        block = request["document_blocks"][0]
        quotes = [("core", request["chunk"]["text"])]
        if request.get("neighbor_context"):
            # 故意把同 Block 的邻文当成依据，验证框架会剔除它和依赖它的对象。
            quotes.append(("outside", request["neighbor_context"][0]["text"]))
        data = {
            "evidence": [{"evidence_id": key, "block_id": block["block_id"],
                          "locator": block["locator"], "quote": text} for key, text in quotes],
            "features": [{"feature_id": key, "name": text, "description": "",
                          "evidence_ids": [key]} for key, text in quotes],
            "rules": [{"rule_id": key, "feature_id": key, "kind": "other",
                       "statement": text, "evidence_ids": [key]} for key, text in quotes],
        }
        return ModelResult(output=output_schema.model_validate(data))


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_same_block_background_is_scoped_in_real_pipeline(tmp_path, background, monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得联网"))
    )
    cfg = AppConfig.model_validate({
        "project": {"workdir": str(tmp_path / "work")},
        "storage": {"database": str(tmp_path / "data.sqlite")},
        "chunking": {
            "strategy": "structural_v2", "target_tokens": 12, "overlap_tokens": 0,
            "token_counter": "character_approx", "split_long_paragraphs": True,
            "extraction_context_blocks": int(background), "extraction_context_chars": 1000,
        },
        "models": {"embedding": {"model": "fixture-embedding"}},
        "retrieval": {"embedding_dimension": 3},
    })
    path = tmp_path / "fixture.docx"
    doc = Document()
    doc.add_paragraph("管理员才能提交。提交后进入待处理状态。撤销时恢复原状态。失败后保留原内容。")
    doc.save(path)
    model = ScopedLLM()
    pipeline = build_pipeline(cfg, "run_scoped", model, FakeEmbedder())
    await pipeline.points(path, "生成测试点", tmp_path / "out", run_id="run_scoped")
    assert len(model.requests) >= 3
    saved = pipeline.read("run_scoped", "extract")
    assert len(saved["evidence"]) == len(model.requests)
    assert len(saved["features"]) == len(saved["rules"]) == len(model.requests)
    assert {e["quote"] for e in saved["evidence"]} == {r["chunk"]["text"] for r in model.requests}
    assert any("邻文不能扩展" in i["description"] for i in saved["issues"]) == background
    for request in model.requests:
        if background:
            assert request["neighbor_context_policy"] == "source_window_v2"
            assert request["neighbor_context"]
            for item in request["neighbor_context"]:
                assert item["scope"] == "same_block"
                assert not any(max(s["start_char"], item["start_char"]) <
                               min(s["end_char"], item["end_char"])
                               for s in request["chunk"]["sources"])
        else:
            assert "neighbor_context" not in request and "neighbor_context_policy" not in request
    assert ("extract_context" in pipeline.manifest("run_scoped")["prompts"]) == background
    assert Path(path).is_file()
