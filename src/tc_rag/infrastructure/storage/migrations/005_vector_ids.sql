-- SQLite 分配显式 int64；同空间/业务对象重复分配保持稳定，不使用 UUID 截断。
CREATE TABLE vector_ids (
    vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    UNIQUE (space_id, object_id)
);
