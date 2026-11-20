"""完整快照先持久化，再在同一 SQLite 事务内切换活跃指针。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from tc_rag.domain.kb.ingestion import IndexedSnapshot, IngestionJob
from tc_rag.domain.models import ModelInvocation


class KnowledgeSqliteRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS kb_heads "
                "(kb_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS kb_snapshots "
                "(snapshot_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS kb_jobs "
                "(job_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS kb_invocations "
                "(invocation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS kb_results "
                "(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS kb_embedding_cache "
                "(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);"
            )

    def active(self, kb_id: str) -> IndexedSnapshot | None:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT s.payload FROM kb_heads h JOIN kb_snapshots s "
                "ON h.snapshot_id=s.snapshot_id WHERE h.kb_id=?", (kb_id,),
            ).fetchone()
        return IndexedSnapshot.model_validate_json(row[0]) if row else None

    def save_job(self, job: IngestionJob) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO kb_jobs VALUES (?, ?)",
                (job.job_id, job.model_dump_json()),
            )

    def snapshot(self, kb_id: str, snapshot_id: str) -> IndexedSnapshot | None:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM kb_snapshots WHERE snapshot_id=?", (snapshot_id,),
            ).fetchone()
        value = IndexedSnapshot.model_validate_json(row[0]) if row else None
        return value if value and value.snapshot.kb_id == kb_id else None

    def save_result(self, run_id: str, payload: dict[str, Any]) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO kb_results VALUES (?, ?)",
                (run_id, json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )

    def cached_embedding(self, key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM kb_embedding_cache WHERE cache_key=?", (key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_embedding(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO kb_embedding_cache VALUES (?, ?)",
                (key, json.dumps(payload, allow_nan=False)),
            )
            row = connection.execute(
                "SELECT payload FROM kb_embedding_cache WHERE cache_key=?", (key,),
            ).fetchone()
            if row is None:
                raise ValueError("向量缓存写入失败")
            stored = json.loads(row[0])
            if not isinstance(stored, dict):
                raise ValueError("向量缓存必须是对象")
            return stored

    def save_invocation(self, invocation: ModelInvocation) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO kb_invocations VALUES (?, ?)",
                (invocation.invocation_id, invocation.model_dump_json()),
            )

    def publish(self, value: IndexedSnapshot, expected: str | None) -> None:
        snapshot = value.snapshot
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT snapshot_id FROM kb_heads WHERE kb_id=?", (snapshot.kb_id,),
            ).fetchone()
            current = row[0] if row else None
            if current != expected:
                raise ValueError("知识库已被其他任务更新，请重新导入")
            connection.execute(
                "INSERT OR IGNORE INTO kb_snapshots VALUES (?, ?)",
                (snapshot.snapshot_id, value.model_dump_json()),
            )
            connection.execute(
                "INSERT OR REPLACE INTO kb_heads VALUES (?, ?)",
                (snapshot.kb_id, snapshot.snapshot_id),
            )
