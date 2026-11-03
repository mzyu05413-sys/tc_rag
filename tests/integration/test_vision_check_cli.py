"""视觉验收默认离线，读主库、写独立产物，不要求生成或向量端点。"""

import io
import json

import yaml
from PIL import Image
from typer.testing import CliRunner

from tc_rag.domain.models import DocumentBlock, Locator, Run
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.infrastructure.storage.files import LocalArtifactStore
from tc_rag.infrastructure.storage.sqlite import SqliteRepository
from tc_rag.interfaces.cli import app


def test_prepare_never_builds_models_and_preserves_source_database(tmp_path, monkeypatch):
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
    result = CliRunner().invoke(app, [
        "vision", "--run-id", "run", "--config", str(config_path),
        "--output-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "out").glob("*/results.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["summary"]["model_calls"] == 0 and not report["summary"]["live"]
    assert report["summary"]["business_accuracy"] is None
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
