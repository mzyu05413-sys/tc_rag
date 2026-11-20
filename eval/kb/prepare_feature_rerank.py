"""冻结两批功能任务的向量/混合重排池；不调用模型。"""

import argparse
import hashlib
import json
from pathlib import Path

from tc_rag.core.config import load_config


def prepare(base: Path, snapshot_path: Path, output: Path, model: str):
    if output.exists():
        raise ValueError("输出目录已存在")
    def read(path):
        return json.loads(path.read_text("utf-8"))
    snapshot = read(snapshot_path)
    sid = snapshot["snapshot"]["snapshot_id"]
    chunks = {c["chunk_id"]: c for c in snapshot["chunks"]}
    jobs, inputs = [], [snapshot_path]
    for batch, run_name, gold_name, policy in [
        ("round1", "baseline_v1", "gold_v1", "body_only"),
        ("round2", "expansion_baseline_v1", "expansion_multimodal_gold_v1", "body_and_image"),
    ]:
        gold = base / "annotations" / gold_name
        candidate_path = base / "runs" / run_name / "candidates.json"
        results = read(candidate_path)
        scope = read(gold / "scope_manifest.json")
        tasks = {t["task_id"]: t for t in read(gold / "tasks.reviewed.json")}
        labels = read(gold / "labels.reviewed.json")
        if scope["snapshot_id"] != sid or not scope.get("gold_complete"):
            raise ValueError("快照或金标完整性不符")
        for tid, task in tasks.items():
            subset = [r for r in labels if r["query_id"] == tid]
            allowed = scope["question_scopes"][tid]
            if (task["split"] != "dev" or len(subset) != len(allowed)
                    or len(allowed) != len(set(allowed))
                    or {r["chunk_id"] for r in subset} != set(allowed)):
                raise ValueError("仅接受完整审核的dev范围")
            if any(r["review_status"] != "reviewed" or r["snapshot_id"] != sid
                   or type(r["label"]) is not int or r["label"] not in (0, 1)
                   or r["text_sha256"] != hashlib.sha256(
                       chunks[r["chunk_id"]]["text"].encode("utf-8")).hexdigest() for r in subset):
                raise ValueError("标签未审核、未知或正文哈希不符")
        inputs.extend([candidate_path, gold / "scope_manifest.json",
                       gold / "labels.reviewed.json", gold / "tasks.reviewed.json"])
        for mode in ("vector", "hybrid"):
            selected = [r for r in results if r["mode"] == mode]
            if (len(selected) != len(tasks)
                    or {r["query_id"] for r in selected} != set(tasks)):
                raise ValueError("检索结果缺题或重复")
            pools = []
            for row in selected:
                tid = row["query_id"]
                candidates = row["candidates"]
                ids = [c["chunk_id"] for c in candidates]
                if (row["snapshot_id"] != sid or row["question"] != tasks[tid]["request"]
                        or not 0 < len(ids) <= 40 or len(ids) != len(set(ids))
                        or not set(ids) <= set(scope["question_scopes"][tid])
                        or any(c != chunks[c["chunk_id"]] for c in candidates)):
                    raise ValueError("候选内容、顺序来源或范围不符")
                if len(row["question"]) > 4096 or any(len(c["text"]) > 4096 for c in candidates):
                    raise ValueError("文本超过重排限制，禁止静默裁剪")
                pools.append(dict(query_id=tid, question=row["question"], split="dev",
                                  candidates=candidates))
            jobs.append(dict(job_id=f"{batch}_{mode}", batch=batch, mode=mode,
                             gold_directory=str(gold), evidence_policy=policy,
                             payload=dict(snapshot_id=sid, expected_model=model,
                                          expected_tie_policy="input_order",
                                          input_policy="original_frozen_chunk_text",
                                          questions=pools)))
    # 所有检查完成后才落盘，原有结果和金标均不覆盖。
    output.mkdir(parents=True)
    manifest_jobs = []
    for job in jobs:
        payload = job.pop("payload")
        path = output / job["job_id"] / "pool.json"
        path.parent.mkdir()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        manifest_jobs.append({**job, "pool_path": str(path),
                              "pool_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "query_count": len(payload["questions"]),
                              "candidate_counts": [len(p["candidates"])
                                                   for p in payload["questions"]]})
    (output / "plan.json").write_text(json.dumps(dict(
        status="prepared_not_executed", expected_model=model, tie_policy="input_order",
        k_values=[3, 5, 10], jobs=manifest_jobs, snapshot_id=sid,
        source_hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
        limits=["两批金标口径不同，禁止合并总体指标", "固定池只能改善排序，不能找回池外证据",
                "真实请求仅重排，供应商可能分批或重试"]),
        ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    args = parser.parse_args()
    settings = load_config(args.config)
    prepare(args.base, args.snapshot, args.output, settings.models.rerank.model)
