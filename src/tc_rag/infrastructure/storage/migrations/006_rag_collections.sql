-- 按业务类别隔离事实，仍采用领域对象 JSON 编码；kind 约束防止写错集合。
CREATE TABLE embedding_spaces (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_extract(payload,'$.kind')='space')
);
CREATE TABLE embedding_records (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_extract(payload,'$.kind')='embedding')
);
CREATE TABLE requirement_sets (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_extract(payload,'$.kind')='requirement_set')
);
CREATE TABLE index_manifests (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_extract(payload,'$.kind')='manifest')
);
CREATE TABLE context_bundles (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_extract(payload,'$.kind')='context')
);
INSERT INTO embedding_spaces SELECT id,payload FROM rag_records WHERE json_extract(payload,'$.kind')='space';
INSERT INTO embedding_records SELECT id,payload FROM rag_records WHERE json_extract(payload,'$.kind')='embedding';
INSERT INTO requirement_sets SELECT id,payload FROM rag_records WHERE json_extract(payload,'$.kind')='requirement_set';
INSERT INTO index_manifests SELECT id,payload FROM rag_records WHERE json_extract(payload,'$.kind')='manifest';
INSERT INTO context_bundles SELECT id,payload FROM rag_records WHERE json_extract(payload,'$.kind')='context';
DELETE FROM rag_records WHERE json_extract(payload,'$.kind') IN ('space','embedding','requirement_set','manifest','context');
