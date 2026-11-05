"""分块验收只读取业务库，派生对照不调用模型、不泄漏配置凭据。"""

import json

import pytest
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
        "--attach-headings",
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


@pytest.mark.parametrize("stored,flags,expected", [
    ({}, [], {"attach_headings": False, "min_tokens": 0, "split_long_paragraphs": False}),
    ({"attach_headings": True, "min_tokens": 20, "split_long_paragraphs": True}, [],
     {"attach_headings": True, "min_tokens": 20, "split_long_paragraphs": True}),
    ({"attach_headings": True, "min_tokens": 20, "split_long_paragraphs": True},
     ["--keep-headings", "--min-tokens", "0", "--keep-long-paragraphs"],
     {"attach_headings": False, "min_tokens": 0, "split_long_paragraphs": False}),
    ({}, ["--attach-headings", "--min-tokens", "20", "--split-long-paragraphs"],
     {"attach_headings": True, "min_tokens": 20, "split_long_paragraphs": True}),
])
def test_compare_inherits_historical_settings_and_only_explicit_flags_override(
    tmp_path, stored, flags, expected,
):
    repo, snapshot, config = prepare(tmp_path)
    run = repo.get("runs", "run", Run)
    run.config_snapshot["chunking"].update(stored)
    repo.save("runs", "run", run)
    # 当前 YAML 的选项故意与历史相反；对照历史运行时不能偷用今天的配置。
    current = yaml.safe_load(config.read_text(encoding="utf-8"))
    current["chunking"] = {"attach_headings": not expected["attach_headings"]}
    config.write_text(yaml.safe_dump(current), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), "--compare", *flags,
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "out").glob("*/statistics.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["comparison"]["options"] == {"strategy": "structural", **expected}
    assert bool(report["comparison"]["overrides"]) == bool(flags)
    assert "同章节相邻重叠" in result.output and "跨一级章节/跨文档块" in result.output
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
    assert repo.get("runs", "run", Run) == run


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


def test_v2_and_neighbor_preview_are_read_only_and_inherit_history(tmp_path):
    repo, snapshot, config = prepare(tmp_path)
    run = repo.get("runs", "run", Run)
    run.config_snapshot["chunking"].update(
        strategy="structural_v2", extraction_context_blocks=1, extraction_context_chars=100
    )
    repo.save("runs", "run", run)
    result = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), "--compare",
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "out").glob("*/statistics.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["comparison"]["options"]["strategy"] == "structural_v2"
    preview = json.loads(path.with_name("extraction_context.json").read_text(encoding="utf-8"))
    assert preview["options"] == {"blocks_per_side": 1, "total_chars": 100}
    assert any(c["neighbor_context"] for c in preview["chunks"])
    for c in preview["chunks"]:
        own = {s["block_id"] for s in c["allowed_sources"]}
        assert all(b["block_id"] not in own for b in c["neighbor_context"])
    assert "secret-sentinel" not in path.with_name("extraction_context.json").read_text("utf-8")
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
    assert repo.get("runs", "run", Run) == run
    invalid = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), "--compare", "--strategy", "bytes",
    ])
    assert invalid.exit_code == 2


def test_budget_matrix_and_pending_review_preserve_historical_budget(tmp_path, monkeypatch):
    repo, snapshot, config = prepare(tmp_path)
    monkeypatch.setattr(
        "httpx.AsyncClient.send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得联网"))
    )
    result = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), "--compare",
        "--strategy", "structural_v2", "--target-tokens", "20", "--overlap-tokens", "2",
        "--targets", "8", "--targets", "20", "--overlaps", "0", "--overlaps", "2",
        "--prepare-gold", "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    directory = next((tmp_path / "out").iterdir())
    report = json.loads((directory / "statistics.json").read_text(encoding="utf-8"))
    assert report["target_tokens"] == 100 and report["overlap_tokens"] == 0
    trial = report["comparison"]
    assert trial["target_tokens"] == 20 and trial["overlap_tokens"] == 2
    assert report["coverage"]["source_character_coverage"] == 1
    matrix = json.loads((directory / "budget_matrix.json").read_text(encoding="utf-8"))
    assert len(matrix["variants"]) == 4 and matrix["selected_variant"] is None
    assert all(v["gold"]["boundary_completeness"] is None for v in matrix["variants"])
    labels = json.loads((directory / "gold.review.json").read_text(encoding="utf-8"))
    assert all(u["status"] == "pending" for u in labels["units"])
    assert "必须保存。" in (directory / "gold_review.md").read_text(encoding="utf-8")
    assert "未评估" in (directory / "budget_matrix.md").read_text(encoding="utf-8")
    assert not any("secret-sentinel" in f.read_text(encoding="utf-8") for f in directory.iterdir())
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
    assert repo.get("runs", "run", Run).config_snapshot["chunking"]["target_tokens"] == 100


def test_confirmed_boundary_labels_are_reused_and_wrong_version_fails(tmp_path):
    from tc_rag.application.services.chunk_gold import candidate_review

    repo, snapshot, config = prepare(tmp_path)
    blocks = [DocumentBlock.model_validate(b) for b in snapshot.payload["blocks"]]
    gold = candidate_review("run", blocks)
    gold.units[0].status = "confirmed"
    source = tmp_path / "labels.json"
    source.write_text(gold.model_dump_json(indent=2), encoding="utf-8")
    source_bytes = source.read_bytes()
    args = [
        "chunk-stats", "run", "--config", str(config), "--gold", str(source), "--compare",
        "--output-dir", str(tmp_path / "out"),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "out").glob("*/statistics.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["gold"]["confirmed_units"] == 1
    assert report["comparison"]["gold"]["boundary_completeness"] == 1
    assert source.read_bytes() == source_bytes
    gold.block_fingerprint = "wrong-version"
    source.write_text(gold.model_dump_json(indent=2), encoding="utf-8")
    assert CliRunner().invoke(app, args).exit_code == 2
    assert len(list((tmp_path / "out").glob("*/statistics.json"))) == 1
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot


@pytest.mark.parametrize("flags", [
    ["--target-tokens", "20"],
    ["--context-blocks", "1"],
    ["--context-chars", "100"],
    ["--compare", "--target-tokens", "20", "--overlap-tokens", "20"],
    ["--compare", "--targets", "20", "--targets", "100", "--overlaps", "25"],
    ["--compare", "--targets", "20", "--min-tokens", "20"],
])
def test_invalid_comparison_budgets_do_not_create_artifact_directories(tmp_path, flags):
    _, _, config = prepare(tmp_path)
    result = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), *flags,
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 2
    assert not (tmp_path / "out").exists()
    assert "secret-sentinel" not in result.output


def test_same_block_context_preview_has_readable_ranges_and_keeps_database_readonly(tmp_path):
    repo, snapshot, config = prepare(tmp_path)
    block = DocumentBlock.model_validate(snapshot.payload["blocks"][-1])
    block.content = "管理员才能提交。提交后进入待处理状态。撤销时恢复原状态。失败后保留原内容。"
    block.processing_version = None
    snapshot.payload["blocks"] = [block.model_dump(mode="json")]
    snapshot.payload["chunks"] = [
        c.model_dump(mode="json") for c in structural_chunks([block], 100, 0).chunks
    ]
    repo.save("stage_snapshots", "run:chunk", snapshot)
    result = CliRunner().invoke(app, [
        "chunk-stats", "run", "--config", str(config), "--compare",
        "--strategy", "structural_v2", "--target-tokens", "12", "--overlap-tokens", "0",
        "--split-long-paragraphs", "--context-blocks", "1", "--context-chars", "100",
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    directory = next((tmp_path / "out").iterdir())
    preview = json.loads((directory / "extraction_context.json").read_text("utf-8"))
    assert preview["policy"] == "source_window_v2" and preview["model_calls"] == 0
    assert len(preview["chunks"]) >= 3
    text = (directory / "extraction_context.md").read_text("utf-8")
    assert "可引用正文" in text and "仅作背景（不可引用）" in text
    assert "不能把本页当成实际模型输入" in text
    for chunk in preview["chunks"]:
        assert chunk["neighbor_context"]
        for item in chunk["neighbor_context"]:
            assert item["scope"] == "same_block" and item["text"] in text
            assert f"[{item['start_char']}, {item['end_char']})" in text
    assert "secret-sentinel" not in text + result.output
    assert repo.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
