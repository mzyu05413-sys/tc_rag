"""采纳显式业务审核并生成独立的片段预标；二者审核状态严格分开。"""

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def apply(draft: Path, review: Path, decisions: Path, snapshot: Path, scope: Path, output: Path):
    if output.exists():
        raise ValueError("输出已存在")
    tasks = json.loads(draft.read_text("utf-8"))
    evidence_review = review.read_text("utf-8")
    adopted = json.loads(decisions.read_text("utf-8"))
    data = json.loads(snapshot.read_text("utf-8"))
    protocol = json.loads(scope.read_text("utf-8"))
    sid = data["snapshot"]["snapshot_id"]
    if protocol["snapshot_id"] != sid or any(t["snapshot_id"] != sid for t in tasks):
        raise ValueError("快照不匹配")
    if set(adopted["task_decisions"]) != {t["task_id"] for t in tasks}:
        raise ValueError("审核任务不完整或含额外任务")
    blocks = {b["block_id"]: b for b in data["blocks"]}
    receipt = {"reviewer": adopted["reviewer"], "date": adopted["review_date"],
        "source": "user_edited_review_file", "review_file_sha256": sha(review.read_bytes()),
        "draft_sha256": sha(draft.read_bytes()), "decisions_sha256": sha(decisions.read_bytes()),
        "independent_second_annotation": False,
        "approval_scope": "task_boundaries_and_rule_meanings_only_not_chunk_labels"}
    for task in tasks:
        decision = adopted["task_decisions"][task["task_id"]]
        header = "## " + task["feature"]
        if evidence_review.count(header) != 1:
            raise ValueError("审核章节不唯一")
        section = evidence_review.split(header, 1)[1].split("\n## ", 1)[0]
        if decision["feedback_quote"] not in section:
            raise ValueError("审核意见与原文件不一致")
        expected_texts = [task["request"], *task["in_scope"], *task["out_of_scope"],
                          *(r["statement"] for r in task["rules"])]
        if any(text not in section for text in expected_texts):
            raise ValueError("审核正文与草稿不同，需显式整理业务修改")
        known_rules = {r["rule_id"] for r in task["rules"]}
        if not set(decision.get("rule_updates", {})) <= known_rules:
            raise ValueError("修改了未知规则")
        for rule in task["rules"]:
            rule["statement"] = decision.get("rule_updates", {}).get(
                rule["rule_id"], rule["statement"])
            rule["review_status"] = "reviewed"
        for addition in decision.get("add_rules", []):
            if addition["rule_id"] in known_rules:
                raise ValueError("新增规则ID重复")
            block = blocks[f"{task['document_version']}:{addition['source_order']}"]
            if block["origin"] != "text" or block["kind"] == "image":
                raise ValueError("新增规则无直接正文")
            ids = [c["chunk_id"] for c in data["chunks"] if any(
                s["block_id"] == block["block_id"] and s["start_char"] == 0
                and s["end_char"] == len(block["content"]) for s in c["sources"])]
            if not ids:
                raise ValueError("新增规则未完整映射索引")
            task["rules"].append({"rule_id": addition["rule_id"],
                "statement": addition["statement"], "review_status": "reviewed",
                "evidence": [{"block_id": block["block_id"], "start_char": 0,
                    "end_char": len(block["content"]), "quote": block["content"],
                    "content_sha256": sha(block["content"].encode("utf-8")),
                    "locator": block["locator"], "origin": "text", "anchor_chunk_ids": ids}]})
            known_rules.add(addition["rule_id"])
        task.update(review_status="reviewed", business_review=deepcopy(receipt),
                    clarifications=decision["clarifications"], questions=decision["open_questions"],
                    excluded_clarifications=decision["excluded"], relevance_review_status="pending",
                    scope_complete=False, formal_metrics=None)

    labels, cards, summaries = [], [], []
    annotation = adopted["annotation_decisions"]
    for task in tasks:
        tid = task["task_id"]
        allowed = set(protocol["question_scopes"][tid])
        positives = set(annotation["positive_chunk_orders"][tid])
        negatives = set(annotation["negative_direct_text_chunk_orders"][tid])
        if positives & negatives:
            raise ValueError("正负例决策冲突")
        stats = {"task_id": tid, "total": 0, "positive": 0, "direct_text": 0, "vision_only": 0}
        observed_positive = set()
        observed_negative = set()
        for position, chunk in enumerate(data["chunks"]):
            if chunk["chunk_id"] not in allowed:
                continue
            direct = [s for s in chunk["sources"] if blocks[s["block_id"]]["origin"] == "text"
                      and blocks[s["block_id"]]["kind"] != "image"]
            if not direct:
                if position in positives or position in negatives:
                    raise ValueError("正文决策错误地包含纯Vision片段")
                label, reason = 0, "正文主指标排除纯图片/Vision片段，不代表图片内容一定错误"
                stats["vision_only"] += 1
            else:
                stats["direct_text"] += 1
                if position in positives:
                    label, reason = 1, "直接支持已采纳任务的具体规则或明确子场景"
                    observed_positive.add(position)
                elif position in negatives:
                    label, reason = 0, "经助手通读，不直接支持本任务规则；可能是背景或其他功能"
                    observed_negative.add(position)
                else:
                    raise ValueError(f"正文片段尚无显式审核决策：{tid}:{position}")
            supports = []
            for rule in task["rules"]:
                found = [e for e in rule["evidence"] if any(
                    s["block_id"] == e["block_id"] and s["start_char"] <= e["start_char"]
                    and s["end_char"] >= e["end_char"] for s in direct)]
                if found:
                    complete = len(found) == len(rule["evidence"])
                    supports.append({"rule_id": rule["rule_id"],
                                     "coverage": "complete" if complete else "partial"})
            partial = annotation.get("partial_support", {}).get(str(position))
            if partial and partial["task_id"] == tid:
                block_ids = {f"{task['document_version']}:{partial['block_order']}"}
                if (not block_ids <= {s["block_id"] for s in direct}
                        or not set(partial["rule_ids"]) <= {r["rule_id"] for r in task["rules"]}):
                    raise ValueError("补充语义判定缺少对应正文或规则")
                supports.extend({"rule_id": key, "coverage": "partial"}
                                for key in partial["rule_ids"])
                reason = partial["reason"]
            if bool(supports) != bool(label):
                raise ValueError("预标决策与明确规则支持关系不一致")
            row = {"query_id": tid, "chunk_id": chunk["chunk_id"], "chunk_order": position,
                "snapshot_id": sid, "label": label, "review_status": "pending",
                "annotator": "assistant", "reason": reason, "supports": supports,
                "text_sha256": sha(chunk["text"].encode("utf-8"))}
            labels.append(row)
            cards.append({**row, "direct_text": "\n".join(
                blocks[s["block_id"]]["content"][s["start_char"]:s["end_char"]] for s in direct)})
            stats["total"] += 1
            stats["positive"] += label
        if (stats["total"] != len(allowed) or observed_positive != positives
                or observed_negative != negatives):
            raise ValueError("片段范围或正例决策不完整")
        summaries.append(stats)
    output.mkdir(parents=True)
    outputs = {"tasks.reviewed.json": tasks, "review_receipt.json": receipt,
        "labels.prelabel.json": labels, "label_cards.json": cards,
        "annotation_summary.json": {"status": "assistant_prelabels_pending_adoption",
            "summaries": summaries, "formal_metrics": None}}
    for name, payload in outputs.items():
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    (output / "review_source.md").write_bytes(review.read_bytes())
    (output / "scope_manifest.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("draft", "review", "decisions", "snapshot", "scope", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    apply(args.draft, args.review, args.decisions, args.snapshot, args.scope, args.output)
