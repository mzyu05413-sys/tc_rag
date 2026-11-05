"""视觉验收默认离线，读主库、写独立产物，不要求生成或向量端点。"""

import io
import json

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from tc_rag.domain.models import DocumentBlock, Locator, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.cli import app


@pytest.mark.parametrize(
    "switch,expected", [(None, True), ("--verify", True), ("--no-verify", False)]
)
def test_prepare_never_builds_models_and_preserves_source_database(
    tmp_path, monkeypatch, switch, expected
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline mode must not create model clients")

    monkeypatch.setattr("tc_rag.interfaces.vision_check.build_vision_service", forbidden)
    store = LocalArtifactStore(tmp_path / "assets")
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), "white").save(buffer, format="PNG")
    asset = store.put("original.png", buffer.getvalue())
    image = DocumentBlock(
        block_id="img", document_id="d", order=0, kind="image", locator=Locator(),
        content=asset.model_dump_json(),
    )
    old = DocumentBlock(
        block_id="img_old", document_id="d", order=1, kind="paragraph", locator=Locator(),
        content="历史描述</pre><script>alert('x')</script>", origin="vision",
        processing_model="legacy", processing_version="vision/v1",
    )
    source = SqliteRepository(tmp_path / "source.sqlite")
    source.save("runs", "run", Run(run_id="run", config_snapshot={}, status="partial_failed"))
    snapshot = StageSnapshot(
        snapshot_id="run:chunk", run_id="run", stage="chunk",
        payload={"blocks": [image.model_dump(mode="json"), old.model_dump(mode="json")]},
    )
    source.save("stage_snapshots", snapshot.snapshot_id, snapshot)
    config_path = tmp_path / "local.yaml"
    config_path.write_text(yaml.safe_dump({
        "project": {"workdir": str(store.root)},
        "storage": {"database": str(source.database)},
    }), encoding="utf-8")
    args = [
        "vision", "--run-id", "run", "--config", str(config_path),
        "--output-dir", str(tmp_path / "out"),
    ]
    result = CliRunner().invoke(app, [*args, *([switch] if switch else [])])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "out").glob("*/results.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["summary"]["model_calls"] == 0 and not report["summary"]["live"]
    assert report["summary"]["business_accuracy"] is None
    assert report["summary"]["processing"]["verify"] is expected
    assert report["summary"]["skipped_images"] == 1
    assert report["summary"]["plan_failed_images"] == 0
    assert report["images"][0]["baseline"] == old.content
    assert source.get("stage_snapshots", "run:chunk", StageSnapshot) == snapshot
    html = reports[0].with_name("review.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert" in html and html.count("<script>") == 1
    assert not reports[0].with_name("vision.sqlite").exists()


def test_missing_snapshot_fails_without_printing_config_credentials(tmp_path):
    source = SqliteRepository(tmp_path / "source.sqlite")
    config = tmp_path / "local.yaml"
    config.write_text(yaml.safe_dump({
        "storage": {"database": str(source.database)},
        "models": {"vision": {"api_key": "secret-sentinel"}},
    }), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "vision", "--run-id", "missing", "--config", str(config),
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 2 and "secret-sentinel" not in result.output


def test_score_saved_results_never_loads_config_or_builds_models(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline scoring cannot load credentials or create models")

    monkeypatch.setattr("tc_rag.interfaces.vision_check.load_config", forbidden)
    config = tmp_path / "not-read.yaml"
    config.write_text("{}", encoding="utf-8")
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"images": [{"number": 1, "result": None}]}), encoding="utf-8")
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps({"images": {"001": {"must_have": ["必填"]}}}), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "vision", "--results", str(results), "--gold", str(gold),
        "--config", str(config), "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "out").glob("*/score.json"))
    score = json.loads(path.read_text(encoding="utf-8"))
    assert score["recall"] is None and score["unavailable_images"] == ["001"]
    denied = CliRunner().invoke(app, [
        "vision", "--results", str(results), "--gold", str(gold), "--live",
        "--config", str(config), "--output-dir", str(tmp_path / "denied"),
    ])
    assert denied.exit_code == 2


def test_live_failure_keeps_report_and_returns_nonzero_with_reason(tmp_path, monkeypatch):
    """报告能生成不代表识别成功；错误在本轮区域展示且经过 HTML 转义。"""
    config = tmp_path / "local.yaml"
    config.write_text("{}", encoding="utf-8")

    def prepare(config, run_id, destination, limit):
        destination.mkdir(parents=True)
        Image.new("RGB", (100, 100), "white").save(destination / "image.png")
        return [], [{
            "number": 1, "block_id": "img", "heading": "登录", "original": "image.png",
            "views": [], "baseline": "旧运行的解释", "result": None, "status": "prepared",
            "checksum": "fixture",
        }]

    async def execute(config, blocks, rows, directory, review_id):
        rows[0].update(status="failed", issues=[{
            "description": "图片解释失败：INVALID_VISUAL_STRUCTURE<script>",
        }])
        return []

    monkeypatch.setattr("tc_rag.interfaces.vision_check.prepare", prepare)
    monkeypatch.setattr("tc_rag.interfaces.vision_check.execute", execute)
    result = CliRunner().invoke(app, [
        "vision", "--run-id", "run", "--config", str(config), "--live",
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 2 and "视觉验收失败" in result.output
    path = next((tmp_path / "out").glob("*/results.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["failed_images"] == 1 and report["summary"]["completed"] == 0
    html = path.with_name("review.html").read_text(encoding="utf-8")
    assert "图片解释失败：INVALID_VISUAL_STRUCTURE&lt;script&gt;" in html
    assert "历史解释仅供对照，不代表本轮成功" in html and html.count("<script>") == 1
