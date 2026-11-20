"""固定池前后配对评分；服务失败单列，禁止改候选集合。"""

import argparse
import hashlib
import json
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion, metrics


def validate_pair(pool, row):
    expected = [c["chunk_id"] for c in pool["candidates"]]
    if row["before"] != expected or len(row["after"]) != len(expected):
        raise ValueError("原排名或返回数量改变")
    if len(set(row["after"])) != len(expected) or set(row["after"]) != set(expected):
        raise ValueError("候选集合改变")


def score(pool_path: Path, result_path: Path, gold_path: Path, output: Path,
          allow_holdout: bool = False, scope_manifest: Path | None = None):
    if output.exists():
        raise ValueError("评分输出已存在")
    pool_data = json.loads(pool_path.read_text("utf-8"))
    pools = {p["query_id"]: p for p in pool_data["questions"]}
    if not pools or len(pools) != len(pool_data["questions"]):
        raise ValueError("问题集为空或ID重复")
    protocol = json.loads(scope_manifest.read_text("utf-8")) if scope_manifest else None
    if protocol is not None and protocol.get("snapshot_id") != pool_data["snapshot_id"]:
        raise ValueError("范围协议与候选池快照不一致")
    allowed = "test" if allow_holdout else "dev"
    if any(p["split"] != allowed for p in pools.values()):
        raise ValueError("集合不匹配")
    if allow_holdout and pool_data.get("experiment_kind") != "holdout_supplement":
        raise ValueError("缺少留出补充对照声明")
    digest = hashlib.sha256(pool_path.read_bytes()).hexdigest()
    rows = [json.loads(line) for line in result_path.read_text("utf-8").splitlines() if line]
    if len(rows) != len(pools) or {r["query_id"] for r in rows} != set(pools):
        raise ValueError("结果缺题或重复")
    labels = json.loads(gold_path.read_text("utf-8"))
    if any(r["review_status"] != "reviewed" for r in labels):
        raise ValueError("标签未批准")
    if any(type(r["label"]) is not int or r["label"] not in {0, 1} for r in labels):
        raise ValueError("标签存在未知或非法值，不能作为负例")
    if any(r.get("snapshot_id") != pool_data["snapshot_id"] for r in labels):
        raise ValueError("标签与候选池快照不一致")
    universes = {}
    for qid, pool in pools.items():
        subset = [r for r in labels if r["query_id"] == qid]
        universe = {r["chunk_id"] for r in subset}
        if len(subset) != len(universe) or not universe:
            raise ValueError("金标重复或为空")
        if protocol is None:
            if len(universe) != 150:
                raise ValueError("非历史150片段实验必须提供范围协议")
        else:
            expected = protocol.get("question_scopes", {}).get(qid, protocol.get("chunk_ids"))
            if (not isinstance(expected, list) or not expected
                    or len(expected) != len(set(expected)) or set(expected) != universe):
                raise ValueError("金标全集与范围协议不一致")
        candidate_ids = [c["chunk_id"] for c in pool["candidates"]]
        if len(candidate_ids) != len(set(candidate_ids)) or not set(candidate_ids) <= universe:
            raise ValueError("候选池重复或超出金标全集")
        universes[qid] = universe
    scored, errors = [], []
    for row in rows:
        qid = row["query_id"]
        if row["pool_sha256"] != digest:
            raise ValueError("候选池指纹改变")
        if row["status"] != "success":
            errors.append(row)
            continue
        validate_pair(pools[qid], row)
        subset = [r for r in labels if r["query_id"] == qid]
        universe = universes[qid]
        relevant = {r["chunk_id"] for r in subset if r["label"] == 1}
        for mode in ("before", "after"):
            for k in (1, 3, 5, 10, 20):
                ids = row[mode][:k]
                counts = confusion(universe, relevant, ids)
                first = next((i for i, key in enumerate(ids, 1) if key in relevant), None)
                rr = (1/first if first else 0) if relevant else None
                scored.append(dict(query_id=qid, mode=mode, k=k,
                                   **counts, **metrics(counts), rr=rr))
    summary = []
    for mode in ("before", "after"):
        for k in (1, 3, 5, 10, 20):
            selected = [r for r in scored if r["mode"] == mode and r["k"] == k]
            names = ("precision", "recall", "f1", "accuracy", "rr")
            valid = {key: [r[key] for r in selected if r[key] is not None] for key in names}
            total = {key: sum(r[key] for r in selected) for key in ("tp", "fp", "fn", "tn")}
            summary.append(dict(mode=mode, k=k, n=len(selected), macro={
                key: sum(values)/len(values) if values else None for key, values in valid.items()
            }, valid_counts={key: len(values) for key, values in valid.items()},
                counts=total, micro=metrics(total)))
    output.mkdir(parents=True)
    (output / "scores.json").write_text(json.dumps({
        "rows": scored, "summary": summary, "errors": errors, "paired_n": len(rows)-len(errors),
        "pool_sha256": digest, "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "results_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "scope_policy": "explicit_manifest" if protocol is not None else "legacy_approval_150",
        "scope_manifest_sha256": hashlib.sha256(scope_manifest.read_bytes()).hexdigest()
        if scope_manifest else None,
        "universe_sizes": {qid: len(ids) for qid, ids in universes.items()},
        "no_positive_query_ids": [qid for qid in pools if not any(
            r["query_id"] == qid and r["label"] == 1 for r in labels)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 固定候选池重排前后对照", "",
             f"{allowed}固定池补充对照；配对成功{len(rows)-len(errors)}题，失败{len(errors)}题。失败单列不填零。",
             "", "| 路线 | k | 宏P | 宏R | 宏F1 | MRR@k |", "|---|---:|---:|---:|---:|---:|"]
    for r in summary:
        def fmt(value):
            return f"{value:.3f}" if value is not None else "未知"
        lines.append(f"| {r['mode']} | {r['k']} | " + " | ".join(
            fmt(r["macro"][key]) for key in ("precision", "recall", "f1", "rr")) + " |")
    (output / "先看这里_重排前后对照.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pool", "results", "gold", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--allow-holdout", action="store_true")
    parser.add_argument("--scope-manifest", type=Path)
    args = parser.parse_args()
    score(args.pool, args.results, args.gold, args.output, args.allow_holdout, args.scope_manifest)
