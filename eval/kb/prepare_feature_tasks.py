"""准备功能任务与规则证据草稿；不运行检索、不生成用例或正式金标。"""

import argparse
import hashlib
import json
from pathlib import Path


def prepare(snapshot_path: Path, seed_path: Path, output: Path, *, split: str = "dev"):
    if output.exists():
        raise ValueError("输出已存在")
    if split not in {"dev", "test"}:
        raise ValueError("集合只能为dev或test")
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    seed = json.loads(seed_path.read_text("utf-8"))
    if not seed or len({t["task_id"] for t in seed}) != len(seed):
        raise ValueError("任务为空或ID重复")
    documents = {d["document_id"]: d for d in snapshot["snapshot"]["documents"]}
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    tasks, scopes = [], {}
    lines = [f"# 功能任务审核：共{len(seed)}项", "",
        ("这是独立验证候选草稿，尚未冻结或发布为金标；未查看检索结果。"
         if split == "test" else "这是开发试标草稿，不是留出集，也未发布为金标。"),
        "每个任务单独限定到其PRD。",
        "只需确认功能范围、规则含义和待确认项；无需填写片段ID或公式。", ""]
    for task in seed:
        document = documents[task["document_id"]]
        version = document["version_id"]
        scope = [c["chunk_id"] for c in snapshot["chunks"] if all(
            blocks[s["block_id"]]["document_id"] == version for s in c["sources"])]
        scopes[task["task_id"]] = scope
        rules = []
        for rule in task["rules"]:
            evidence = []
            for anchor in rule["anchors"]:
                block = blocks[f"{version}:{anchor['order']}"]
                if block["origin"] != "text" or block["kind"] == "image":
                    raise ValueError("规则依据必须是直接正文")
                quote = anchor.get("quote", block["content"])
                if block["content"].count(quote) != 1:
                    raise ValueError("规则引文未唯一匹配")
                start = block["content"].index(quote)
                end = start + len(quote)
                if any(start < r["end_char"] and r["start_char"] < end
                       for r in block.get("deprecated_ranges", [])):
                    raise ValueError("规则引用作废文字")
                ids = [c["chunk_id"] for c in snapshot["chunks"] if c["chunk_id"] in scope
                    and any(s["block_id"] == block["block_id"] and s["start_char"] <= start
                            and s["end_char"] >= end for s in c["sources"])]
                if not ids:
                    raise ValueError("规则依据未完整落在索引片段中")
                evidence.append({"block_id": block["block_id"], "start_char": start,
                    "end_char": end, "quote": quote, "locator": block["locator"],
                    "content_sha256": hashlib.sha256(block["content"].encode("utf-8")).hexdigest(),
                    "anchor_chunk_ids": ids, "origin": "text"})
            rules.append({"rule_id": rule["rule_id"], "statement": rule["statement"],
                          "review_status": "pending", "evidence": evidence})
        tasks.append({**{k: v for k, v in task.items() if k != "rules"}, "rules": rules,
            "split": split, "review_status": "pending", "scope_complete": False,
            "snapshot_id": snapshot["snapshot"]["snapshot_id"], "document_version": version,
            "document_sha256": document["content_sha256"], "retrieval_chunk_count": len(scope),
            "annotation_source": "assistant_prd_draft", "formal_metrics": None})
        lines.extend([f"## {task['feature']}", "", f"任务：{task['request']}",
            f"PRD：{task['document_id']}；本任务检索范围：{len(scope)}个片段。", "",
            "包含：" + "；".join(task["in_scope"]), "不包含：" + "；".join(task["out_of_scope"]),
            "", "### 规则与原文依据", ""])
        for index, rule in enumerate(rules, 1):
            lines.extend([f"{index}. {rule['statement']}", ""])
            for e in rule["evidence"]:
                lines.extend(["> " + e["quote"], ""])
        lines.extend(["### 需要确认", ""])
        lines.extend("- " + q for q in task["questions"])
        lines.extend(["", "反馈：认可范围／修改范围；规则有误请说明；未确定的细则可继续保留。", ""])
    output.mkdir(parents=True)
    (output / "tasks.draft.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), "utf-8")
    (output / "scope_manifest.json").write_text(json.dumps({
        "snapshot_id": snapshot["snapshot"]["snapshot_id"], "question_scopes": scopes,
        "status": f"{split}_scope_pending_business_review", "gold_complete": False,
        "split": split,
        "snapshot_file_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2), "utf-8")
    review_name = "先看这里_三个功能审核.md" if len(seed) == 3 else "先看这里_功能审核.md"
    (output / review_name).write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "seed", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    args = parser.parse_args()
    prepare(args.snapshot, args.seed, args.output, split=args.split)
