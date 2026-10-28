-- 核心事实以带 schema_version 的 JSON 保存；引用列与关联表提供数据库外键保护。
CREATE TABLE documents (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE document_blocks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE chunks (id TEXT PRIMARY KEY, sources TEXT NOT NULL CHECK(json_valid(sources)), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE evidence_spans (id TEXT PRIMARY KEY, block_id TEXT NOT NULL REFERENCES document_blocks(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE features (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE requirement_rules (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE test_points (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE test_cases (id TEXT PRIMARY KEY, steps TEXT NOT NULL CHECK(json_valid(steps)), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE coverage_links (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES test_cases(id),
    rule_id TEXT NOT NULL REFERENCES requirement_rules(id),
    point_id TEXT NOT NULL REFERENCES test_points(id),
    step_no INTEGER NOT NULL CHECK(step_no > 0),
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    UNIQUE(case_id, step_no, rule_id, point_id)
);
CREATE TABLE issues (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE model_invocations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL CHECK(json_valid(payload)));

-- 多对多关系单独保存；删除所有者会清理关联，删除被引用事实则默认拒绝。
CREATE TABLE chunk_blocks (owner_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES document_blocks(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE feature_evidence (owner_id TEXT NOT NULL REFERENCES features(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES evidence_spans(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE rule_evidence (owner_id TEXT NOT NULL REFERENCES requirement_rules(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES evidence_spans(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE point_rules (owner_id TEXT NOT NULL REFERENCES test_points(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES requirement_rules(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE case_points (owner_id TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES test_points(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE case_rules (owner_id TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES requirement_rules(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE TABLE case_evidence (owner_id TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE, ref_id TEXT NOT NULL REFERENCES evidence_spans(id), position INTEGER NOT NULL, PRIMARY KEY(owner_id, position));
CREATE INDEX blocks_document_idx ON document_blocks(document_id);
CREATE INDEX evidence_block_idx ON evidence_spans(block_id);
CREATE INDEX invocations_run_idx ON model_invocations(run_id);
