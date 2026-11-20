"""固定两路已保存排名，仅对比RRF向量权重1和2；不请求模型。"""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion, metrics
from tc_rag.infrastructure.retrieval.keyword import rrf

covers = runpy.run_path(str(Path(__file__).with_name("run_feature_baseline.py")))["covers"]


def fuse(vector, keyword, vector_weight):
    if type(vector_weight) is not int or vector_weight not in (1, 2):
        raise ValueError("本实验仅允许向量权重1或2")
    # 重复传入是整数权重的等价实现，不代表新增独立召回源。
    return rrf([vector] * vector_weight + [keyword], limit=40, constant=60)


def compare(run, gold, snapshot_path, output):
    if output.exists():
        raise ValueError("输出目录已存在")
    def read(path):
        return json.loads(path.read_text("utf-8"))
    original = read(run / "candidates.json")
    original_scores = read(run / "scores.json")
    protocol = read(run / "complete.json")
    scope = read(gold / "scope_manifest.json")
    tasks = {t["task_id"]: t for t in read(gold / "tasks.reviewed.json")}
    labels = read(gold / "labels.reviewed.json")
    snapshot = read(snapshot_path)
    sid = snapshot["snapshot"]["snapshot_id"]
    if (sid != scope["snapshot_id"] or sid != original_scores["snapshot_id"]
            or not scope.get("gold_complete") or protocol["budget"] != 40
            or protocol["rrf_constant"] != 60 or protocol["rerank"]
            or protocol["keyword_algorithm"] != "bm25"
            or any(t["split"] != "dev" for t in tasks.values())):
        raise ValueError("实验配置或范围协议不符")
    expected = {(tid, mode) for tid in tasks for mode in ("vector", "keyword", "hybrid")}
    if (len(original) != len(expected)
            or {(r["query_id"], r["mode"]) for r in original} != expected):
        raise ValueError("原始结果缺题或重复")
    chunks = {c["chunk_id"]: c for c in snapshot["chunks"]}
    universes, positive, body = {}, {}, {}
    for tid in tasks:
        subset = [r for r in labels if r["query_id"] == tid]
        ids = scope["question_scopes"][tid]
        if len(ids) != len(set(ids)) or len(subset) != len(ids) or {
                r["chunk_id"] for r in subset} != set(ids):
            raise ValueError("逐题完整标签不符")
        if any(r["review_status"] != "reviewed" or r["snapshot_id"] != sid
               or type(r["label"]) is not int or r["label"] not in (0, 1)
               or type(r["body_label"]) is not int or r["body_label"] not in (0, 1)
               or r["text_sha256"] != hashlib.sha256(
                   chunks[r["chunk_id"]]["text"].encode("utf-8")).hexdigest() for r in subset):
            raise ValueError("标签状态、类型或正文哈希不符")
        universes[tid] = set(ids)
        positive[tid] = {r["chunk_id"] for r in subset if r["label"]}
        body[tid] = {r["chunk_id"] for r in subset if r["body_label"]}
    rows, rankings, rule_ranks = [], [], []
    for tid, task in tasks.items():
        group = [r for r in original if r["query_id"] == tid]
        baseline = next(r for r in group if r["mode"] == "hybrid")
        vector, keyword = baseline["vector_ranking"], baseline["keyword_ranking"]
        if (any(r["snapshot_id"] != sid or r["vector_ranking"] != vector
                or r["keyword_ranking"] != keyword for r in group)
                or any(len(route) > 40 or len(route) != len(set(route))
                       or not set(route) <= universes[tid] for route in (vector, keyword))):
            raise ValueError("两路排名不一致或非法")
        for weight in (1, 2):
            fused = fuse(vector, keyword, weight)
            ids = [cid for cid, _ in fused]
            if weight == 1 and ids != [c["chunk_id"] for c in baseline["candidates"]]:
                raise ValueError("1:1不能逐项复现基线排名")
            ranking = [chunks[cid] for cid in ids]
            rankings.append(dict(query_id=tid, vector_weight=weight, keyword_weight=1,
                                 chunk_ids=ids, fusion_scores=dict(fused)))
            for rule in task["rules"]:
                first = next((k for k in range(1, len(ranking)+1)
                              if all(covers(e, ranking[:k]) for e in rule["evidence"])), None)
                rule_ranks.append(dict(query_id=tid, rule_id=rule["rule_id"],
                                       vector_weight=weight, first_complete_rank=first))
            for k in (3, 5, 10):
                selected = ranking[:k]
                selected_ids = ids[:k]
                complete = [r["rule_id"] for r in task["rules"]
                            if r["evidence"] and all(covers(e, selected) for e in r["evidence"])]
                if weight == 1:
                    old = next(r for r in original_scores["rows"] if r["query_id"] == tid
                               and r["mode"] == "hybrid" and r["k"] == k)
                    counts = confusion(universes[tid], body[tid], selected_ids)
                    if (any(old[key] != value for key, value in counts.items())
                            or set(complete) != set(old["complete_rule_ids"])):
                        raise ValueError("原规则与正文指标不能复现")
                counts = confusion(universes[tid], positive[tid], selected_ids)
                first = next((i for i, cid in enumerate(selected_ids, 1)
                              if cid in positive[tid]), None)
                rows.append(dict(query_id=tid, vector_weight=weight, k=k, **counts,
                                 **metrics(counts), rr=(1/first if first else 0)
                                 if positive[tid] else None, returned_count=len(selected_ids),
                                 complete_rule_ids=complete, rule_total=len(task["rules"]),
                                 rule_coverage=len(complete)/len(task["rules"])))
    summary = []
    for weight in (1, 2):
        for k in (3, 5, 10):
            selected = [r for r in rows if r["vector_weight"] == weight and r["k"] == k]
            total = {key: sum(r[key] for r in selected) for key in ("tp", "fp", "fn", "tn")}
            valid = {key: [r[key] for r in selected if r[key] is not None]
                     for key in ("precision", "recall", "f1", "rr", "rule_coverage")}
            summary.append(dict(vector_weight=weight, k=k, counts=total, n=len(selected),
                                macro={key: sum(v)/len(v) if v else None
                                       for key, v in valid.items()}, micro=metrics(total),
                                valid_counts={key: len(v) for key, v in valid.items()},
                                complete_rules=sum(len(r["complete_rule_ids"]) for r in selected),
                                rule_total=sum(r["rule_total"] for r in selected)))
    files = [run / "candidates.json", run / "scores.json", run / "complete.json",
             gold / "labels.reviewed.json", gold / "tasks.reviewed.json",
             gold / "scope_manifest.json", snapshot_path]
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps(dict(
        status="dev_single_variable_weight_comparison", snapshot_id=sid, model_calls=0,
        vector_weights=[1, 2], keyword_weight=1, budget=40, rrf_constant=60, rerank=False,
        rows=rows, summary=summary, rankings=rankings, rule_ranks=rule_ranks,
        baseline_exact_reproduction=True, hashes={str(p): hashlib.sha256(
            p.read_bytes()).hexdigest() for p in files},
        rule_policy="original_body_complete_evidence_no_full_image_substitution"),
        ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "gold", "snapshot", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    compare(args.run, args.gold, args.snapshot, args.output)
