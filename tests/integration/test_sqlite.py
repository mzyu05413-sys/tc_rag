"""使用临时数据库和虚构对象检查迁移、引用完整性及并发短事务。"""

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from tc_rag.domain import models as m
from tc_rag.infrastructure.storage.sqlite import TABLES, SqliteRepository, object_key


def records():
    """按外键依赖顺序准备十二类事实，不读取用户文档。"""
    loc = m.Locator(page=1)
    values = [
        m.Document(document_id="d", name="虚构文档", content_hash="hash", asset_id="a"),
        m.DocumentBlock(
            block_id="b",
            document_id="d",
            kind="paragraph",
            order=0,
            content="虚构原文",
            locator=loc,
        ),
        m.Chunk(
            chunk_id="c",
            text="虚构原文",
            token_estimate=4,
            fingerprint="fp",
            sources=[m.ChunkSource(block_id="b", start_char=0, end_char=4)],
        ),
        m.EvidenceSpan(evidence_id="e", block_id="b", locator=loc, quote="虚构原文"),
        m.Feature(feature_id="f", name="演示", description="描述", evidence_ids=["e"]),
        m.RequirementRule(
            rule_id="r", feature_id="f", kind="other", statement="演示规则", evidence_ids=["e"]
        ),
        m.TestPoint(point_id="p", rule_ids=["r"], technique="other", objective="演示目的"),
        m.TestCase(
            case_id="t",
            point_ids=["p"],
            title="演示用例",
            steps=[m.TestStep(action="操作", expected="预期", rule_ids=["r"], evidence_ids=["e"])],
        ),
        m.CoverageLink(case_id="t", step_no=1, rule_id="r", point_id="p"),
        m.Issue(issue_id="i", kind="gap", severity="warning", description="演示问题"),
        m.Run(run_id="run", config_snapshot={}, status="queued"),
        m.ModelInvocation(
            invocation_id="inv", run_id="run", stage="demo", provider="fake", model="fake"
        ),
    ]
    return [(table, object_key(value), value) for table, value in zip(TABLES, values)]


def test_migration_idempotent_and_all_models_roundtrip(tmp_path):
    path = tmp_path / "data.sqlite"
    repo = SqliteRepository(path)
    repo.migrate()
    repo.save_many(records())
    # 重开仓库证明数据没有只保存在内存对象中。
    reopened = SqliteRepository(path)
    for table, key, value in records():
        assert reopened.get(table, key, type(value)) == value
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 6
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(TABLES) <= tables
        sources = json.loads(connection.execute("SELECT sources FROM chunks").fetchone()[0])
        assert sources["schema_version"] == "1.0"
        assert sources["items"][0]["end_char"] == 4
        steps = json.loads(connection.execute("SELECT steps FROM test_cases").fetchone()[0])
        assert steps["items"][0]["evidence_ids"] == ["e"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_key_and_batch_rollback(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    first = records()[0]
    bad = m.EvidenceSpan(evidence_id="bad", block_id="missing", quote="演示", locator=m.Locator())
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_many([first, ("evidence_spans", "bad", bad)])
    assert repo.get("documents", "d", m.Document) is None
    repo.save_many(records())
    with pytest.raises(sqlite3.IntegrityError):
        repo.delete("evidence_spans", "e")
    repo.delete("coverage_links", records()[8][1])
    assert repo.get("coverage_links", records()[8][1], m.CoverageLink) is None


def test_update_replaces_links_and_failed_update_restores_old_data(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    repo.save_many(records())
    feature = m.Feature(feature_id="f", name="更新", description="演示", evidence_ids=["missing"])
    with pytest.raises(sqlite3.IntegrityError):
        repo.save("features", "f", feature)
    assert repo.get("features", "f", m.Feature).name == "演示"
    feature.evidence_ids = ["e"]
    repo.save("features", "f", feature)
    assert repo.get("features", "f", m.Feature).name == "更新"


def test_bad_collection_key_and_schema(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    with pytest.raises(ValueError):
        repo.save("documents", "wrong", records()[0][2])
    with pytest.raises(ValueError):
        repo.get("documents", "d", m.Run)
    with pytest.raises(ValueError):
        repo.delete("documents; DROP TABLE runs", "d")
    assert repo.get("documents", "missing", m.Document) is None


def test_concurrent_writers(tmp_path):
    path = tmp_path / "data.sqlite"
    repo = SqliteRepository(path)

    def write(index):
        run = m.Run(run_id=f"run_{index}", config_snapshot={}, status="queued")
        # 每个线程重新装配仓库，同时检查迁移锁与写入锁的协调。
        SqliteRepository(path).save("runs", run.run_id, run)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    assert all(repo.get("runs", f"run_{i}", m.Run) is not None for i in range(12))


def test_migration_failure_rolls_back_and_checksum_detects_changes(tmp_path):
    import tc_rag.infrastructure.storage.sqlite as module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    source = module.Path(module.__file__).with_name("migrations") / "001_initial.sql"
    shutil.copyfile(source, migrations / source.name)
    path = tmp_path / "data.sqlite"
    SqliteRepository(path, migrations)
    second = migrations / "002_failure.sql"
    second.write_text("CREATE TABLE should_rollback (id TEXT);\nINVALID SQL;\n", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        SqliteRepository(path, migrations)
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='should_rollback'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    second.unlink()
    (migrations / source.name).write_text(
        source.read_text(encoding="utf-8") + "\n-- 修改\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="发生变化"):
        SqliteRepository(path, migrations)
