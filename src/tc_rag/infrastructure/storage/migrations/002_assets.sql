-- 文件登记同样进入 SQLite；旧版 documents 暂不新增强制非空资产外键，以兼容已有记录。
CREATE TABLE assets (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE INDEX documents_content_hash_idx ON documents(json_extract(payload, '$.content_hash'));
