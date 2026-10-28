"""单一 RAG 完整 CLI 验证；预设输出只证明工程行为，不代表真实检索收益。"""

import json
from zipfile import ZipFile, is_zipfile

import pytest
from typer.testing import CliRunner

from tc_rag.bootstrap import build_pipeline
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.providers.fakes import FakeEmbedder
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces import workflow


@pytest.mark.parametrize("rerank_enabled", [False, True])
@pytest.mark.parametrize("file_keys", [False, True])
def test_rag_joint_export_and_evaluate(tmp_path, monkeypatch, rerank_enabled, file_keys):
    from test_pipeline import FixtureLLM, setup

    original, document, _, _ = setup(tmp_path)
    cfg = original.config.model_copy(deep=True)
    cfg.models.llm.model = "fixture-llm"
    cfg.models.embedding.model = "fixture-embedding"
    cfg.models.rerank.model = "fixture-rerank"
    cfg.models.rerank.base_url = "https://fixture/rerank"
    cfg.retrieval.embedding_dimension = 3

    class Reranker:
        calls = 0

        async def rank(self, query, candidates):
            self.calls += 1
            return list(reversed(candidates))

    reranker = Reranker()
    monkeypatch.setattr(
        workflow,
        "build_pipeline",
        lambda config, run_id: build_pipeline(
            config, run_id, FixtureLLM(), FakeEmbedder(), reranker
        ),
    )
    runner = CliRunner()
    cfg.models.rerank.enabled = rerank_enabled
    path = tmp_path / "rag.json"
    data = cfg.model_dump(mode="json")
    secret = "fixture-two-phase-file-secret"
    if file_keys:
        for role in ("llm", "embedding", "rerank"):
            data["models"][role]["api_key"] = secret
            monkeypatch.delenv(f"TC_RAG_{role.upper()}_API_KEY", raising=False)
    path.write_text(json.dumps(data), encoding="utf-8")
    args = ["--config", str(path)]
    result = runner.invoke(
        workflow.app,
        [
            "run",
            str(document),
            "-i",
            "生成测试点",
            "--output-dir",
            str(tmp_path / "out"),
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    run_id = result.output.split("run_id=")[1].splitlines()[0]
    run_dir = tmp_path / "out" / run_id
    xmind = next(run_dir.glob("testpoints*.xmind"))
    assert xmind.is_file() and list(run_dir.glob("testcases*.xlsx"))
    # 失败重试入口不接收脑图或审核记录，直接使用已保存测试点。
    repeated = runner.invoke(
        workflow.app,
        [
            "cases",
            run_id,
            "-i",
            "生成用例",
            "--output-dir",
            str(tmp_path / "out"),
            *args,
        ],
    )
    assert repeated.exit_code == 0, repeated.output
    generated = json.loads(repeated.stdout)
    assert generated["cases"] == 3
    assert list(xmind.parent.glob("batch_*/testpoints*.xmind"))
    assert list(xmind.parent.glob("batch_*/testcases*.xlsx"))
    repo = SqliteRepository(cfg.storage.database)
    retrieval = repo.get("stage_snapshots", f"{run_id}:retrieval", StageSnapshot)
    assert {b["generation"]["stage"] for b in retrieval.payload["bundles"]} == {
        "plan_points",
        "generate",
    }
    assert all(b["index_version"] for b in retrieval.payload["bundles"])
    extraction = repo.get("stage_snapshots", f"{run_id}:extract", StageSnapshot)
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "rules": [{"rule_id": "gold-300", "statement": "最多300个，数量为整数。"}],
                "rule_groups": [
                    {
                        "generated_ids": [r["rule_id"] for r in extraction.payload["rules"]],
                        "gold_ids": ["gold-300"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "evaluation.json"
    result = runner.invoke(
        workflow.app, ["evaluate", run_id, "--gold", str(gold), "--output", str(output), *args]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["rule_precision"] == report["rule_recall"] == 1
    assert report["evidence_recall_at_k"] is None
    assert "rag_context" in report["audit"]["prompts"]
    assert (reranker.calls > 0) is rerank_enabled
    if file_keys:
        # 原始私有配置允许含密钥；SQLite、审计、报告、脑图和 Excel 均不得复制它。
        for artifact in tmp_path.rglob("*"):
            if not artifact.is_file() or artifact == path:
                continue
            assert secret.encode() not in artifact.read_bytes(), artifact.name
            if is_zipfile(artifact):
                with ZipFile(artifact) as archive:
                    assert all(secret.encode() not in archive.read(n) for n in archive.namelist())
