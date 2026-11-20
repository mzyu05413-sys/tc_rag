"""对同一份已保存排名使用图文金标评分；不检索，不访问模型。"""

import argparse
import hashlib
import json
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion, metrics


def score(run: Path, gold: Path, output: Path):
    if output.exists():
        raise ValueError("输出目录已存在")
    read = lambda path: json.loads(path.read_text("utf-8"))  # noqa: E731
    original = read(run / "scores.json")
    candidates = read(run / "candidates.json")
    labels = read(gold / "labels.reviewed.json")
    scope = read(gold / "scope_manifest.json")
    sid = original["snapshot_id"]
    if scope["snapshot_id"] != sid or not scope.get("gold_complete"):
        raise ValueError("金标快照或完整状态不符")
    if any(r["snapshot_id"] != sid for r in candidates):
        raise ValueError("候选快照不符")
    by_old = {(r["query_id"], r["mode"], r["k"]): r for r in original["rows"]}
    if len(by_old) != len(original["rows"]):
        raise ValueError("旧评分重复")
    expected = {(tid, mode) for tid in scope["question_scopes"]
                for mode in ("vector", "keyword", "hybrid")}
    if {(r["query_id"], r["mode"]) for r in candidates} != expected or len(candidates) != len(
            expected):
        raise ValueError("候选缺题或重复")
    universes, positives, body = {}, {}, {}
    for tid, ids in scope["question_scopes"].items():
        subset = [r for r in labels if r["query_id"] == tid]
        if (len(ids) != len(set(ids)) or len(subset) != len(ids)
                or {r["chunk_id"] for r in subset} != set(ids)):
            raise ValueError("金标全集缺失或重复")
        if any(r["review_status"] != "reviewed" or r["snapshot_id"] != sid
               or type(r["label"]) is not int or r["label"] not in (0, 1)
               or type(r["body_label"]) is not int or r["body_label"] not in (0, 1)
               for r in subset):
            raise ValueError("标签未审核或含未知")
        universes[tid] = set(ids)
        positives[tid] = {r["chunk_id"] for r in subset if r["label"]}
        body[tid] = {r["chunk_id"] for r in subset if r["body_label"]}
        if not body[tid] <= positives[tid]:
            raise ValueError("图文口径不应撤掉已审核正文正例")
    if {r["query_id"] for r in labels} != set(universes):
        raise ValueError("金标含额外任务")
    rows = []
    for result in candidates:
        tid = result["query_id"]
        for k in (1, 3, 5, 10):
            old = by_old[(tid, result["mode"], k)]
            ids = [c["chunk_id"] for c in result["candidates"][:k]]
            old_counts = confusion(universes[tid], body[tid], ids)
            if any(old_counts[key] != old[key] for key in old_counts):
                raise ValueError("原正文计数不能复现，拒绝不同排名配对")
            counts = confusion(universes[tid], positives[tid], ids)
            first = next((i for i, cid in enumerate(ids, 1) if cid in positives[tid]), None)
            rows.append(dict(query_id=tid, mode=result["mode"], k=k, **counts,
                             **metrics(counts), returned_count=len(ids),
                             rr=(1/first if first else 0) if positives[tid] else None,
                             retained_body_complete_rule_ids=old["complete_rule_ids"],
                             rule_total=old["rule_count"]))
    summary = []
    for mode in ("vector", "keyword", "hybrid"):
        for k in (1, 3, 5, 10):
            selected = [r for r in rows if r["mode"] == mode and r["k"] == k]
            counts = {key: sum(r[key] for r in selected) for key in ("tp", "fp", "fn", "tn")}
            valid = {key: [r[key] for r in selected if r[key] is not None]
                     for key in ("precision", "recall", "f1", "accuracy", "rr")}
            summary.append(dict(mode=mode, k=k, n=len(selected), counts=counts,
                                macro={key: sum(v)/len(v) if v else None
                                       for key, v in valid.items()},
                                valid_counts={key: len(v) for key, v in valid.items()},
                                micro=metrics(counts)))
    files = [run / "candidates.json", run / "scores.json", gold / "labels.reviewed.json",
             gold / "scope_manifest.json"]
    payload = dict(status="same_rankings_new_gold_policy", snapshot_id=sid,
                   rows=rows, summary=summary, errors=[], model_calls=0,
                   interpretation="label_policy_change_not_algorithm_improvement",
                   rule_coverage_policy="retained_body_only_no_unapproved_image_full_rule_credit",
                   hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    output.mkdir(parents=True)
    (output / "scores.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "gold", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    score(args.run, args.gold, args.output)
