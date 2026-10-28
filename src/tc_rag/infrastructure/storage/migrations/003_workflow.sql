-- 阶段快照保存各次运行的对象 ID，避免跨运行串读；审核事实不可只留在导出文件中。
CREATE TABLE stage_snapshots (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE point_trees (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE review_snapshots (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE TABLE business_entities (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
CREATE INDEX snapshots_run_idx ON stage_snapshots(run_id);
