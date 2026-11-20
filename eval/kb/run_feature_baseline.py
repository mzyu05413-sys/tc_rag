"""三功能dev基线：复用正式检索组件，分别评分片段和规则覆盖。"""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion, metrics


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def covers(evidence, chunks):
    """多片段的直接正文区间并集完整覆盖证据才命中。"""
    cursor = evidence["start_char"]
    spans = sorted((s["start_char"], s["end_char"])
                   for c in chunks for s in c["sources"]
                   if s["block_id"] == evidence["block_id"])
    for start, end in spans:
        if start > cursor:
            break
        cursor = max(cursor, end)
    return cursor >= evidence["end_char"]


def run(bundle, queries, gold, output):
    if output.exists():
        raise ValueError("输出已存在")
    scope = read(gold / "scope_manifest.json")
    tasks = read(gold / "tasks.reviewed.json")
    labels = read(gold / "labels.reviewed.json")
    questions = read(bundle / "questions.evidence_draft.json")
    snapshot = read(bundle / "snapshot.json")
    sid = snapshot["snapshot"]["snapshot_id"]
    task_map = {t["task_id"]: t for t in tasks}
    if (not scope.get("gold_complete") or scope["snapshot_id"] != sid
            or len(task_map) != len(tasks) or len(questions) != len(tasks)
            or {q["query_id"] for q in questions} != set(task_map)
            or {r["query_id"] for r in labels} != set(task_map)):
        raise ValueError("任务、快照或金标协议不完整")
    chunks = {c["chunk_id"]: c for c in snapshot["chunks"]}
    universes, positives = {}, {}
    for q in questions:
        tid = q["query_id"]
        task = task_map[tid]
        subset = [r for r in labels if r["query_id"] == tid]
        expected = scope["question_scopes"][tid]
        if (q["question"] != task["request"] or q["split"] != "dev"
                or q["document_ids"] != [task["document_id"]]
                or not task["scope_complete"] or task["snapshot_id"] != sid
                or len(expected) != len(set(expected)) or len(subset) != len(expected)
                or {r["chunk_id"] for r in subset} != set(expected)):
            raise ValueError("问题文字、范围或审核状态不匹配")
        for row in subset:
            if (row["review_status"] != "reviewed" or row["snapshot_id"] != sid
                    or type(row["label"]) is not int or row["label"] not in (0, 1)
                    or row["text_sha256"] != hashlib.sha256(
                        chunks[row["chunk_id"]]["text"].encode("utf-8")).hexdigest()):
                raise ValueError("标签状态、类型或正文哈希非法")
        universes[tid] = set(expected)
        positives[tid] = {r["chunk_id"] for r in subset if r["label"]}
    runner = runpy.run_path(str(Path(__file__).with_name("run_modes.py")))["run"]
    runner(bundle, queries, output, k=40, budget=40,
           filter_punctuation=True, keyword_algorithm="bm25")
    candidates = read(output / "candidates.json")
    rows = []
    for result in candidates:
        tid = result["query_id"]
        if (len({c["chunk_id"] for c in result["candidates"]}) != len(result["candidates"])
                or not {c["chunk_id"] for c in result["candidates"]} <= universes[tid]):
            raise ValueError("候选重复或超出审核全集")
        for k in (1, 3, 5, 10):
            selected = result["candidates"][:k]
            ids = [c["chunk_id"] for c in selected]
            counts = confusion(universes[tid], positives[tid], ids)
            first = next((i for i, cid in enumerate(ids, 1) if cid in positives[tid]), None)
            rules = task_map[tid]["rules"]
            complete = [r["rule_id"] for r in rules
                        if r["evidence"] and all(covers(e, selected) for e in r["evidence"])]
            rows.append(dict(query_id=tid, mode=result["mode"], k=k, **counts,
                             **metrics(counts), returned_count=len(ids),
                             missing_slots=max(0, k-len(ids)),
                             rr=(1/first if first else 0) if positives[tid] else None,
                             complete_rule_ids=complete, rule_count=len(rules),
                             rule_coverage=len(complete)/len(rules) if rules else None))
    summary = []
    for mode in ("vector", "keyword", "hybrid"):
        for k in (1, 3, 5, 10):
            selected = [r for r in rows if r["mode"] == mode and r["k"] == k]
            counts = {key: sum(r[key] for r in selected) for key in ("tp", "fp", "fn", "tn")}
            names = ("precision", "recall", "f1", "accuracy", "rr", "rule_coverage")
            valid = {key: [r[key] for r in selected if r[key] is not None] for key in names}
            summary.append(dict(mode=mode, k=k, n=len(selected), counts=counts,
                                macro={key: sum(v)/len(v) if v else None
                                       for key, v in valid.items()},
                                valid_counts={key: len(v) for key, v in valid.items()},
                                micro=metrics(counts),
                                returned_count=sum(r["returned_count"] for r in selected),
                                rule_covered=sum(len(r["complete_rule_ids"]) for r in selected),
                                rule_total=sum(r["rule_count"] for r in selected)))
    files = [queries, gold / "labels.reviewed.json", gold / "tasks.reviewed.json",
             gold / "scope_manifest.json", bundle / "manifest.partial.json"]
    payload = dict(status="dev_feature_baseline_completed", snapshot_id=sid, rows=rows,
                   summary=summary, hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in files},
                   rule_policy="all_original_text_evidence_intervals_covered_no_clarification",
                   errors=[], model_calls=0)
    (output / "scores.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "queries", "gold", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    run(args.bundle, args.queries, args.gold, args.output)
