"""开发集精确同分诊断；派生反事实排名，不请求模型、不改原采集。"""

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def stable_ties(row):
    """只对完全相等的数值分数使用原召回次序；不舍入、不使用近似阈值。"""
    before, after, scores = row["before"], row["after"], row["scores"]
    if (len(before) != len(set(before)) or len(after) != len(before)
            or len(set(after)) != len(after) or set(before) != set(after)
            or set(scores) != set(before)):
        raise ValueError("候选ID或分数映射不完整")
    if any(type(v) not in {int, float} or not math.isfinite(v) for v in scores.values()):
        raise ValueError("分数非法")
    if any(scores[a] < scores[b] for a, b in zip(after, after[1:])):
        raise ValueError("采集结果未按分数降序")
    # Python稳定排序使精确同分保持before顺序；不读取金标决定顺序。
    return sorted(before, key=lambda key: -scores[key])


def audit(pool_path: Path, results_path: Path, output: Path):
    if output.exists():
        raise ValueError("诊断输出已存在")
    raw_pool, raw_results = pool_path.read_bytes(), results_path.read_bytes()
    pool = json.loads(raw_pool)
    questions = {q["query_id"]: q for q in pool["questions"]}
    if not questions or any(q.get("split") != "dev" for q in questions.values()):
        raise ValueError("仅允许开发集诊断")
    rows = [json.loads(line) for line in raw_results.decode("utf-8").splitlines() if line]
    if len(rows) != len(questions) or {r["query_id"] for r in rows} != set(questions):
        raise ValueError("采集缺题或重复")
    digest = hashlib.sha256(raw_pool).hexdigest()
    derived, details = [], []
    for row in rows:
        q = questions[row["query_id"]]
        if row["pool_sha256"] != digest or row["before"] != [
            c["chunk_id"] for c in q["candidates"]
        ]:
            raise ValueError("采集指纹或原始候选次序不匹配")
        if row["status"] != "success":
            raise ValueError("本诊断要求完整成功的配对采集，服务失败需单独处理")
        after = stable_ties(row)
        scores = row["scores"]
        counts = Counter(scores.values())
        boundaries = []
        for k in (1, 3, 5, 10, 20):
            if k < len(after) and scores[after[k - 1]] == scores[after[k]]:
                boundaries.append(k)
        details.append({
            "query_id": row["query_id"], "distinct_scores": len(counts),
            "largest_tie_group": max(counts.values()),
            "score_exactly_one_count": counts.get(1.0, 0),
            "top_score_count": counts[max(counts)], "tie_crossing_k": boundaries,
            "changed_top5": row["after"][:5] != after[:5],
            "positions": [{"chunk_id": key, "score": scores[key],
                           "before": row["before"].index(key) + 1,
                           "observed": row["after"].index(key) + 1,
                           "stable": after.index(key) + 1} for key in row["before"]],
        })
        derived.append({**row, "after": after,
                        "experiment_kind": "offline_exact_tie_counterfactual",
                        "derived_from_results_sha256": hashlib.sha256(raw_results).hexdigest()})
    output.mkdir(parents=True)
    (output / "counterfactual_results.jsonl").write_text("".join(
        json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in derived), "utf-8")
    (output / "audit.json").write_text(json.dumps({
        "policy": "exact_score_ties_use_original_rank_v1",
        "pool_sha256": digest,
        "source_results_sha256": hashlib.sha256(raw_results).hexdigest(),
        "n": len(rows), "queries": details,
        "limitation": "离线反事实，非新增模型实测；模型服务的同分来源未知；未使用阈值",
    }, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pool", "results", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    audit(args.pool, args.results, args.output)
