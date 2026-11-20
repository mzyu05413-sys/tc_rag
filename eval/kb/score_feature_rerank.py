"""按两批各自金标评分固定池重排，并独立复算必要正文规则覆盖。"""

import argparse
import hashlib
import json
import math
import runpy
from pathlib import Path

score_pool = runpy.run_path(str(Path(__file__).with_name("score_rerank.py")))["score"]
covers = runpy.run_path(str(Path(__file__).with_name("run_feature_baseline.py")))["covers"]


def score(plan_path, runs, output):
    if output.exists():
        raise ValueError("输出已存在")
    def read(path):
        return json.loads(path.read_text("utf-8"))
    plan = read(plan_path)
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != read(runs / "execution.json")[
            "plan_sha256"]:
        raise ValueError("执行计划指纹不符")
    for path, digest in plan["source_hashes"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("原始材料已改变")
    prepared = []
    for job in plan["jobs"]:
        pool_path = Path(job["pool_path"])
        digest = hashlib.sha256(pool_path.read_bytes()).hexdigest()
        complete = read(runs / job["job_id"] / "complete.json")
        if (digest != job["pool_sha256"] or complete["pool_sha256"] != digest
                or complete["model"] != plan["expected_model"]
                or complete["tie_policy"] != plan["tie_policy"]
                or complete["input_policy"] != "original_frozen_chunk_text"):
            raise ValueError("模型、同分策略或固定池不符")
        pool = read(pool_path)
        result_path = runs / job["job_id"] / "results.jsonl"
        rows = [json.loads(line) for line in result_path.read_text("utf-8").splitlines() if line]
        expected = {q["query_id"]: q for q in pool["questions"]}
        if len(rows) != len(expected) or {r["query_id"] for r in rows} != set(expected):
            raise ValueError("实际结果缺题或重复")
        for row in rows:
            if row["pool_sha256"] != digest or row["tie_policy"] != plan["tie_policy"]:
                raise ValueError("逐题指纹或同分策略不同")
            if row["status"] != "success":
                continue
            ids = [c["chunk_id"] for c in expected[row["query_id"]]["candidates"]]
            if (row["before"] != ids or len(row["after"]) != len(ids)
                    or set(row["after"]) != set(ids) or set(row["scores"]) != set(ids)
                    or any(not math.isfinite(s) for s in row["scores"].values())):
                raise ValueError("候选集合或分数非法")
            stable = sorted(ids, key=lambda cid: (-row["scores"][cid], ids.index(cid)))
            if row["after"] != stable:
                raise ValueError("得分排序或精确同分保序不符")
        prepared.append((job, pool, result_path, rows))
    groups = []
    for job, pool, result_path, results in prepared:
        gold = Path(job["gold_directory"])
        group_out = output / job["job_id"]
        score_pool(Path(job["pool_path"]), result_path, gold / "labels.reviewed.json",
                   group_out, scope_manifest=gold / "scope_manifest.json")
        scored = read(group_out / "scores.json")
        tasks = {t["task_id"]: t for t in read(gold / "tasks.reviewed.json")}
        pools = {q["query_id"]: q for q in pool["questions"]}
        rule_rows, details = [], []
        for row in results:
            if row["status"] != "success":
                continue
            tid = row["query_id"]
            chunks = {c["chunk_id"]: c for c in pools[tid]["candidates"]}
            for stage in ("before", "after"):
                for k in (3, 5, 10):
                    selected = [chunks[cid] for cid in row[stage][:k]]
                    rules = tasks[tid]["rules"]
                    found = [r["rule_id"] for r in rules if r["evidence"]
                             and all(covers(e, selected) for e in r["evidence"])]
                    rule_rows.append(dict(query_id=tid, stage=stage, k=k,
                                          complete_rule_ids=found, rule_total=len(rules)))
                for rule in tasks[tid]["rules"]:
                    first = next((k for k in range(1, len(row[stage])+1) if all(
                        covers(e, [chunks[cid] for cid in row[stage][:k]])
                        for e in rule["evidence"])), None)
                    details.append(dict(query_id=tid, stage=stage, rule_id=rule["rule_id"],
                                        first_complete_rank=first))
        summary = []
        for base in scored["summary"]:
            if base["k"] not in (3, 5, 10):
                continue
            selected = [r for r in rule_rows if r["stage"] == base["mode"] and r["k"] == base["k"]]
            complete = sum(len(r["complete_rule_ids"]) for r in selected)
            total = sum(r["rule_total"] for r in selected)
            summary.append({**base, "complete_rules": complete, "rule_total": total,
                            "macro_rule_coverage": sum(len(r["complete_rule_ids"])/r["rule_total"]
                                for r in selected)/len(selected) if selected else None})
        (group_out / "rules.json").write_text(json.dumps(dict(
            rows=rule_rows, first_complete_rank=details,
            policy="complete_original_body_evidence_no_full_image_substitution"),
            ensure_ascii=False, indent=2), "utf-8")
        groups.append(dict(job_id=job["job_id"], batch=job["batch"], mode=job["mode"],
                           evidence_policy=job["evidence_policy"], paired_n=scored["paired_n"],
                           errors=scored["errors"], summary=summary,
                           highest_score_tie_counts=[dict(query_id=r["query_id"],
                               count=sum(s == max(r["scores"].values())
                                         for s in r["scores"].values()))
                               for r in results if r["status"] == "success"]))
    files = [plan_path, *[p for j in plan["jobs"] for p in (
        runs / j["job_id"] / "results.jsonl", runs / j["job_id"] / "complete.json")]]
    (output / "comparison.json").write_text(json.dumps(dict(
        status="paired_actual_rerank_results", groups=groups, scoring_model_calls=0,
        limits=["两批正文/图文口径不同，禁止合并", "固定候选池不改善池外召回",
                "model字段是配置名称，不证明供应商底层模型版本"],
        hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}),
        ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "runs", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    score(args.plan, args.runs, args.output)
