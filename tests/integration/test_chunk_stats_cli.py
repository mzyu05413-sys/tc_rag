"""分块验收只读取业务库，派生对照不调用模型、不泄漏配置凭据。"""

import json

import yaml
from typer.testing import CliRunner

from tc_rag.application.services.chunking import structural_chunks
from tc_rag.domain.models import DocumentBlock, Locator, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.cli import app


def prepare(tmp_path):
    blocks = [
        DocumentBlock(
            block_id=f"b{i}",
            document_id="d",
            order=i,
            kind=kind,
            content=text,
            heading_path=path,
            locator=Locator(paragraph_index=i),
        )
        for i, (kind, text, path) in enumerate(
            [
                ("heading", "模块", ["模块"]),
                ("heading", "小节", ["模块", "小节"]),
                ("paragraph", "必须保存。", ["模块", "小节"]),
            ]
        )
    ]
    repo = SqliteRepository(tmp_path / "data.sqlite")
    repo.save(
        "runs",
        "run",
        Run(
            run_id="run",
            status="partial_failed",
            config_snapshot={
                "chunking": {
                    "target_tokens": 100,
                    "overlap_tokens": 0,
                    "token_counter": "character_approx",
                },
            },
        ),
    )
    snapshot = StageSnapshot(
        snapshot_id="run:chunk",
        run_id="run",
        stage="chunk",
        payload={
            "blocks": [b.model_dump(mode="json") for b in blocks],
            "chunks": [c.model_dump(mode="json") for c in structural_chunks(blocks, 100, 0).chunks],
        },
    )
    repo.save("stage_snapshots", "run:chunk", snapshot)
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "storage": {"database": str(repo.database)},
                "models": {"llm": {"api_key": "secret-sentinel"}},
            }
        ),
        encoding="utf-8",
    )
    return repo, snapshot, config


def test_compare_preserves_source_and_writes_complete_review(tmp_path, monkeypatch):
    repo, snapshot, config = prepare(tmp_path)
    monkeypatch.setattr(
        "httpx.AsyncClient.send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得联网"))
    )
    args = [
        "chunk-stats",
        "run",
        "--config",
        str(config),
        "--compare",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "out").glob("*/statistics.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["model_calls"] == 0
    assert report["statistics"]["chunks"] == 2
    assert report["comparison"]["statistics"]["chunks"] == 1
    review = path.with_name("review.md").read_text(encoding="utf-8")
    assert "原分块" in review and "试分块" in review and "必须保存。" in review
    assert "secret-sentinel" not in path.read_text(encoding="utf-8") + review + result.output
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
    assert CliRunner().invoke(app, args).exit_code == 0
    assert len(list((tmp_path / "out").glob("*/statistics.json"))) == 2


def test_missing_run_and_invalid_budget_fail_without_credentials(tmp_path):
    _, _, config = prepare(tmp_path)
    for arguments in (["missing"], ["run", "--compare", "--min-tokens", "100"]):
        result = CliRunner().invoke(
            app,
            [
                "chunk-stats",
                *arguments,
                "--config",
                str(config),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code == 2
        assert "secret-sentinel" not in result.output
