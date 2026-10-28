"""标准库 SQLite 适配器：显式迁移、连接级外键和批量短事务。"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tc_rag.application.ports.contracts import T
from tc_rag.domain import models
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import StageSnapshot
from tc_rag.domain.rag import RagRecord
from tc_rag.domain.review import PointTree, ReviewSnapshot

# 白名单同时限制表名插值和对象类型，调用者不能传入任意 SQL 标识符。
TABLES: dict[str, tuple[type[BaseModel], str]] = {
    "documents": (models.Document, "document_id"),
    "document_blocks": (models.DocumentBlock, "block_id"),
    "chunks": (models.Chunk, "chunk_id"),
    "evidence_spans": (models.EvidenceSpan, "evidence_id"),
    "features": (models.Feature, "feature_id"),
    "requirement_rules": (models.RequirementRule, "rule_id"),
    "test_points": (models.TestPoint, "point_id"),
    "test_cases": (models.TestCase, "case_id"),
    "coverage_links": (models.CoverageLink, ""),
    "issues": (models.Issue, "issue_id"),
    "runs": (models.Run, "run_id"),
    "model_invocations": (models.ModelInvocation, "invocation_id"),
    "assets": (models.Asset, "asset_id"),
    "rag_records": (RagRecord, "record_id"),
    "embedding_spaces": (RagRecord, "record_id"),
    "embedding_records": (RagRecord, "record_id"),
    "requirement_sets": (RagRecord, "record_id"),
    "index_manifests": (RagRecord, "record_id"),
    "context_bundles": (RagRecord, "record_id"),
    "stage_snapshots": (StageSnapshot, "snapshot_id"),
    "point_trees": (PointTree, "tree_id"),
    "review_snapshots": (ReviewSnapshot, "review_id"),
    "business_entities": (models.BusinessEntity, "entity_id"),
}
REFERENCES = {
    "document_blocks": "document_id",
    "evidence_spans": "block_id",
    "requirement_rules": "feature_id",
    "model_invocations": "run_id",
    "stage_snapshots": "run_id",
    "point_trees": "run_id",
    "review_snapshots": "run_id",
}


def object_key(value: BaseModel) -> str:
    """CoverageLink 没有独立 ID，使用四元组的稳定指纹作为仓库键。"""
    if isinstance(value, models.CoverageLink):
        return fingerprint([value.case_id, value.step_no, value.rule_id, value.point_id])
    for model, field in TABLES.values():
        if isinstance(value, model):
            return str(getattr(value, field))
    raise ValueError("该领域类型尚未接入存储")


class SqliteRepository:
    """每次操作独立连接；调用结束即关闭，绝不让写事务跨越模型请求。"""

    def __init__(self, database: str | Path, migrations_dir: Path | None = None) -> None:
        if str(database) == ":memory:":
            raise ValueError("仓库使用短连接，请使用文件数据库而非 :memory:")
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = migrations_dir or Path(__file__).with_name("migrations")
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> None:
        """按编号应用 SQL 文件；校验历史指纹，拒绝覆盖已经执行过的迁移。"""
        files = sorted(
            self.migrations_dir.glob("[0-9]*_*.sql"), key=lambda path: int(path.name.split("_")[0])
        )
        if not files:
            raise ValueError("未找到数据库迁移文件")
        versions = [int(path.name.split("_")[0]) for path in files]
        if versions != list(range(1, len(files) + 1)):
            raise ValueError("迁移序号必须从1开始连续且不能重复")
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            try:
                # 写锁由 SQLite 协调，不同线程和不同仓库实例也共享此保护。
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
                )
                applied = dict(
                    connection.execute("SELECT version, checksum FROM schema_migrations")
                )
                if set(applied) - set(versions):
                    raise ValueError("数据库版本高于当前代码，拒绝继续")
                for version, path in zip(versions, files):
                    sql = path.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode()).hexdigest()
                    if version in applied:
                        if applied[version] != checksum:
                            raise ValueError("已应用的迁移文件发生变化")
                        continue
                    # executescript 会隐式提交，不能用于需要原子回滚的迁移。
                    statement = ""
                    for character in sql:
                        statement += character
                        if character == ";" and sqlite3.complete_statement(statement):
                            connection.execute(statement)
                            statement = ""
                    if statement.strip():
                        # 尾部允许注释或无分号的完整语句；非法语句仍由 SQLite 拒绝。
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations VALUES (?, ?)", (version, checksum)
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def save(self, collection: str, object_id: str, value: BaseModel) -> None:
        """单条写入复用批量事务逻辑。"""
        self.save_many([(collection, object_id, value)])

    def save_many(self, records: Iterable[tuple[str, str, BaseModel]]) -> None:
        """先完成模型校验，再一次短事务写入；调用方按外键依赖顺序传入。"""
        prepared = []
        for table, key, value in records:
            model = self._model(table)
            if not isinstance(value, model):
                raise ValueError("存储集合与领域类型不匹配")
            validated = model.model_validate(value.model_dump())
            if object_key(validated) != key:
                raise ValueError("仓库键与领域对象 ID 不一致")
            prepared.append((table, key, validated.model_dump(mode="json")))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for table, key, data in prepared:
                self._write(connection, table, key, data)

    @staticmethod
    def _model(collection: str) -> type[BaseModel]:
        if collection not in TABLES:
            raise ValueError("未知存储集合")
        return TABLES[collection][0]

    def _write(
        self, connection: sqlite3.Connection, table: str, key: str, data: dict[str, Any]
    ) -> None:
        columns: dict[str, Any] = {"id": key, "payload": json.dumps(data, ensure_ascii=False)}
        if table in REFERENCES:
            field = REFERENCES[table]
            columns[field] = data[field]
        if table in {"chunks", "test_cases"}:
            field = "sources" if table == "chunks" else "steps"
            columns[field] = json.dumps(
                {"schema_version": data["schema_version"], "items": data[field]}, ensure_ascii=False
            )
        if table == "coverage_links":
            columns.update(
                {field: data[field] for field in ("case_id", "rule_id", "point_id", "step_no")}
            )
        names = list(columns)
        updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != "id")
        # 仅标识符来自内部白名单，所有业务值都用占位参数绑定。
        connection.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES "
            f"({', '.join('?' for _ in names)}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(columns.values()),
        )
        links: dict[str, list[str]] = {}
        if table == "chunks":
            links["chunk_blocks"] = [source["block_id"] for source in data["sources"]]
        elif table == "features":
            links["feature_evidence"] = data["evidence_ids"]
        elif table == "requirement_rules":
            links["rule_evidence"] = data["evidence_ids"]
        elif table == "test_points":
            links["point_rules"] = data["rule_ids"]
        elif table == "test_cases":
            links["case_points"] = data["point_ids"]
            for field in ("rule_ids", "evidence_ids"):
                links["case_rules" if field == "rule_ids" else "case_evidence"] = [
                    ref for step in data["steps"] for ref in step[field]
                ]
        for link_table, refs in links.items():
            connection.execute(f"DELETE FROM {link_table} WHERE owner_id=?", (key,))
            connection.executemany(
                f"INSERT INTO {link_table} VALUES (?, ?, ?)",
                [(key, ref, index) for index, ref in enumerate(refs)],
            )

    def get(self, collection: str, object_id: str, schema: type[T]) -> T | None:
        if self._model(collection) is not schema:
            raise ValueError("读取类型与集合不匹配")
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT payload FROM {collection} WHERE id=?", (object_id,)
            ).fetchone()
        return None if row is None else schema.model_validate_json(row[0])

    def delete(self, collection: str, object_id: str) -> None:
        """被其他事实引用时由外键拒绝删除，避免悄悄破坏证据链。"""
        self._model(collection)
        with closing(self._connect()) as connection, connection:
            connection.execute(f"DELETE FROM {collection} WHERE id=?", (object_id,))

    def find_document(self, content_hash: str) -> models.Document | None:
        """按内容指纹复用文件；同内容不同文件名仍属于同一文档版本。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM documents WHERE json_extract(payload, '$.content_hash')=? "
                "ORDER BY id LIMIT 1",
                (content_hash,),
            ).fetchone()
        return None if row is None else models.Document.model_validate_json(row[0])

    def allocate_vector_ids(self, space_id: str, object_ids: list[str]) -> list[int]:
        """事务内登记稳定映射；所有后端共用数据库生成的唯一 int64。"""
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("向量对象 ID 重复")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            result = []
            for key in object_ids:
                connection.execute(
                    "INSERT INTO vector_ids(space_id,object_id) VALUES (?,?) "
                    "ON CONFLICT(space_id,object_id) DO NOTHING",
                    (space_id, key),
                )
                row = connection.execute(
                    "SELECT vector_id FROM vector_ids WHERE space_id=? AND object_id=?",
                    (space_id, key),
                ).fetchone()
                assert row is not None
                result.append(int(row[0]))
            return result
