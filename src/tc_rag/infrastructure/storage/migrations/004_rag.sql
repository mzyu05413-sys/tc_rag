-- RAG 元数据是事实，向量文件是可重建派生物；发布记录只在文件完成校验后写入。
CREATE TABLE rag_records (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
