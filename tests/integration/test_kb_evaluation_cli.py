"""CLI 离线评分只消费已审核标签，保留未知和服务失败。"""

import json

from typer.testing import CliRunner

from tc_rag.interfaces.cli import app


def test_retrieval_evaluation_cli_and_no_overwrite(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    result = {"kb_id": "crm", "snapshot_id": "s1", "query": "规则？", "top_k": 5,
              "document_ids": ["doc"], "candidates": [{"chunk_id": "c", "sources": [{
                  "original_block_id": "b", "content_sha256": "h",
                  "start_char": 0, "end_char": 10,
              }]}]}
    (results / "q.json").write_text(json.dumps(result), encoding="utf-8")
    label = {"query_id": "q", "question": "规则？", "snapshot_id": "s1",
             "document_ids": ["doc"], "review_status": "reviewed", "scope_complete": True,
             "evidence_groups": [[{"block_id": "b", "content_sha256": "h",
                                   "start_char": 0, "end_char": 10}]]}
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps(label), encoding="utf-8")
    output = tmp_path / "report.json"
    arguments = ["kb", "evaluate", "crm", "--gold", str(gold), "--results", str(results),
                 "--output", str(output)]
    runner = CliRunner()
    response = runner.invoke(app, arguments)
    assert response.exit_code == 0, response.output
    report = json.loads(output.read_text("utf-8"))
    assert report["metrics"]["recall"]["mean"] == 1
    assert report["metrics"]["precision"]["mean"] == 0.2
    assert report["metrics"]["recall"]["ci95"] is None
    assert output.with_suffix(".md").exists()
    assert runner.invoke(app, arguments).exit_code != 0


def test_qa_preview_never_calls_model(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("不能装配模型")

    monkeypatch.setattr("tc_rag.bootstrap.kb_session", forbidden)
    runner = CliRunner()
    assert runner.invoke(app, ["kb", "ask", "crm", "-q", "规则？"]).exit_code == 0
    assert runner.invoke(app, ["kb", "chat", "crm"]).exit_code == 0
