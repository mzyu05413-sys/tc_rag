"""利用已保存排名定位必要正文规则与图文补充证据的缺口，不产生新排名。"""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion

covers = runpy.run_path(str(Path(__file__).with_name("run_feature_baseline.py")))["covers"]


def classify_missing(row, selected, candidate, rules):
    missing_rules = [r for r in rules if not all(covers(e, selected) for e in r["evidence"])]
    completes = [r["rule_id"] for r in missing_rules
                 if all(covers(e, [*selected, candidate]) for e in r["evidence"])]
    adds = sorted({r["rule_id"] for r in missing_rules for e in r["evidence"]
                   if not covers(e, selected) and covers(e, [*selected, candidate])})
    if adds:
        category = "adds_missing_required_body_evidence"
    elif row["body_label"]:
        category = "body_evidence_already_covered"
    else:
        missing_ids = {r["rule_id"] for r in missing_rules}
        image_rules = {rid for e in row["image_evidence"] for rid in e["rule_ids"]}
        category = ("partial_image_for_incomplete_rule" if image_rules & missing_ids
                    else "image_subscenario_for_complete_rule")
    return dict(category=category, adds_required_rule_evidence=adds,
                would_complete_rules_if_added=completes)


def diagnose(run, gold, snapshot_path, output):
    if output.exists():
        raise ValueError("输出目录已存在")
    def read(path):
        return json.loads(path.read_text("utf-8"))
    results = read(run / "candidates.json")
    previous = read(run / "scores.json")
    tasks = {t["task_id"]: t for t in read(gold / "tasks.reviewed.json")}
    labels = read(gold / "labels.reviewed.json")
    scope = read(gold / "scope_manifest.json")
    snapshot = read(snapshot_path)
    sid = snapshot["snapshot"]["snapshot_id"]
    if (scope["snapshot_id"] != sid or previous["snapshot_id"] != sid
            or not scope.get("gold_complete")):
        raise ValueError("快照或金标状态不符")
    by_chunk = {c["chunk_id"]: c for c in snapshot["chunks"]}
    by_old = {(r["query_id"], r["mode"], r["k"]): r for r in previous["rows"]}
    if len(by_old) != len(previous["rows"]):
        raise ValueError("旧评分重复")
    expected = {(tid, mode) for tid in tasks for mode in ("vector", "keyword", "hybrid")}
    if (len(results) != len(expected)
            or {(r["query_id"], r["mode"]) for r in results} != expected
            or {r["query_id"] for r in labels} != set(tasks)):
        raise ValueError("任务或模式缺失、重复")
    rows, rules_detail = [], []
    for result in results:
        tid = result["query_id"]
        rules = tasks[tid]["rules"]
        if any(not r["evidence"] for r in rules):
            raise ValueError("必要规则缺少证据")
        subset = [r for r in labels if r["query_id"] == tid]
        ids = scope["question_scopes"][tid]
        if len(subset) != len(ids) or {r["chunk_id"] for r in subset} != set(ids):
            raise ValueError("金标全集不符")
        if any(r["review_status"] != "reviewed" or r["snapshot_id"] != sid
               or type(r["label"]) is not int or r["label"] not in (0, 1)
               for r in subset):
            raise ValueError("标签未审核或非法")
        ranking = result["candidates"]
        ranked_ids = [c["chunk_id"] for c in ranking]
        if len(set(ranked_ids)) != len(ranked_ids) or not set(ranked_ids) <= set(ids):
            raise ValueError("候选重复或越界")
        if any(c != by_chunk[c["chunk_id"]] for c in ranking):
            raise ValueError("候选文本或来源与快照不同")
        for rule in rules:
            first = next((k for k in range(1, len(ranking)+1)
                          if all(covers(e, ranking[:k]) for e in rule["evidence"])), None)
            rules_detail.append(dict(query_id=tid, mode=result["mode"],
                                     rule_id=rule["rule_id"], statement=rule["statement"],
                                     first_complete_rank=first,
                                     ranking_observed_count=len(ranking)))
        body_positive = {r["chunk_id"] for r in subset if r["body_label"]}
        for k in (5, 10):
            selected = ranking[:k]
            selected_ids = {c["chunk_id"] for c in selected}
            counts = confusion(set(ids), body_positive, [c["chunk_id"] for c in selected])
            old = by_old[(tid, result["mode"], k)]
            if any(old[key] != value for key, value in counts.items()):
                raise ValueError("旧正文计数不能复现")
            complete = [r["rule_id"] for r in rules
                        if all(covers(e, selected) for e in r["evidence"])]
            if set(complete) != set(old["complete_rule_ids"]):
                raise ValueError("规则覆盖与已保存评分不同")
            missing = []
            for label in subset:
                cid = label["chunk_id"]
                if label["label"] != 1 or cid in selected_ids:
                    continue
                missing.append(dict(chunk_id=cid, chunk_order=label["chunk_order"],
                                    saved_rank=ranked_ids.index(cid)+1
                                    if cid in ranked_ids else None,
                                    vector_rank=result["vector_ranking"].index(cid)+1
                                    if cid in result["vector_ranking"] else None,
                                    keyword_rank=result["keyword_ranking"].index(cid)+1
                                    if cid in result["keyword_ranking"] else None,
                                    **classify_missing(label, selected, by_chunk[cid], rules)))
            rows.append(dict(query_id=tid, mode=result["mode"], k=k,
                             complete_rules=complete, missing_rules=[r["rule_id"] for r in rules
                                 if r["rule_id"] not in complete],
                             missing_related_chunks=missing))
    files = [run / "candidates.json", run / "scores.json", gold / "labels.reviewed.json",
             gold / "tasks.reviewed.json", gold / "scope_manifest.json", snapshot_path]
    payload = dict(status="diagnostic_only_no_new_ranking", rows=rows, rules=rules_detail,
                   snapshot_id=sid, model_calls=0, hashes={str(p): hashlib.sha256(
                       p.read_bytes()).hexdigest() for p in files},
                   limits=["图片规则关联只是部分支持，不能替代完整正文依据",
                           "未出现于已保存排名不等于全库不存在或完全未召回",
                           "使用金标作失败诊断，不是可部署的排序算法"])
    output.mkdir(parents=True)
    (output / "diagnosis.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "gold", "snapshot", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    diagnose(args.run, args.gold, args.snapshot, args.output)
