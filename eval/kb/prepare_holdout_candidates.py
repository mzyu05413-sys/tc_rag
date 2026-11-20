"""从人工编写的PRD规则草稿绑定快照证据；不检索、不评分、不发布金标。"""

import argparse
import hashlib
import json
from pathlib import Path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def prepare(snapshot_path, spec_path, old_questions_path, old_labels_path, output, manuals=None):
    if output.exists():
        raise ValueError("输出已存在")
    raw = snapshot_path.read_bytes()
    snapshot = json.loads(raw)
    specs = json.loads(spec_path.read_text("utf-8"))
    old_questions = json.loads(old_questions_path.read_text("utf-8"))
    labels = json.loads(old_labels_path.read_text("utf-8"))
    sid = snapshot["snapshot"]["snapshot_id"]
    if not specs or len({q["query_id"] for q in specs}) != len(specs):
        raise ValueError("问题集为空或ID重复")
    old_ids = {q["query_id"] for q in old_questions}
    old_texts = {q["question"].strip() for q in old_questions}
    if any(q["snapshot_id"] != sid for q in old_questions):
        raise ValueError("旧问题集快照不匹配")
    if any(r["snapshot_id"] != sid for r in labels):
        raise ValueError("旧标签快照不匹配")
    old_positive = {r["chunk_id"] for r in labels if r["label"] == 1}
    documents = {d["document_id"]: d for d in snapshot["snapshot"]["documents"]}
    blocks = snapshot["blocks"]
    chunks = snapshot["chunks"]
    questions = []
    for spec in specs:
        if spec["query_id"] in old_ids or spec["question"].strip() in old_texts:
            raise ValueError("问题与旧集重复")
        version = documents[spec["document_id"]]["version_id"]
        evidence = []
        for quote in spec["quotes"]:
            found = [b for b in blocks if b["document_id"] == version
                     and b["origin"] == "text" and b["kind"] != "image"
                     and quote in b["content"]]
            if len(found) != 1:
                raise ValueError(f"证据原文未唯一匹配：{spec['query_id']}")
            block = found[0]
            start = block["content"].index(quote)
            end = start + len(quote)
            if any(start < r["end_char"] and r["start_char"] < end
                   for r in block.get("deprecated_ranges", [])):
                raise ValueError("候选证据包含已标记作废区间")
            supporting = [c["chunk_id"] for c in chunks if any(
                s["block_id"] == block["block_id"] and s["start_char"] <= start
                and s["end_char"] >= end for s in c["sources"])]
            if not supporting:
                raise ValueError("证据没有完整对应的索引片段")
            if set(supporting) & old_positive:
                raise ValueError("候选证据片段与旧正例重叠")
            evidence.append({"block_id": block["block_id"], "locator": block["locator"],
                "content_sha256": digest(block["content"].encode("utf-8")),
                "start_char": start, "end_char": end, "quote": quote,
                "origin": "text", "anchor_chunk_ids": supporting})
        official = None
        if "official_page" in spec:
            if manuals is None:
                raise ValueError("官方依据需要冻结的手册目录")
            captures = json.loads((manuals / "manifest.json").read_text("utf-8"))
            page = next(r for r in captures if r["name"] == spec["official_page"])
            text = (manuals / (page["name"] + ".txt")).read_text("utf-8")
            if digest(text.encode("utf-8")) != page["text_sha256"]:
                raise ValueError("官方正文指纹变化")
            anchor = spec["official_anchor"]
            if text.count(anchor) != 1:
                raise ValueError("官方依据未唯一匹配")
            start = text.index(anchor)
            official = {"url": page["final_url"], "text_sha256": page["text_sha256"],
                "text_file": str(manuals / (page["name"] + ".txt")),
                "start_char": start, "end_char": start + len(anchor),
                "retrieved_utc": page["retrieved_utc"],
                "alignment_status": "assistant_checked_pending_user_review"}
        questions.append({**{k: v for k, v in spec.items()
                             if k not in {"quotes", "official_anchor"}},
            "snapshot_id": sid, "document_version": version,
            "content_sha256": documents[spec["document_id"]]["content_sha256"],
            "split": "proposed_new_holdout", "review_status": "pending",
            "should_abstain": None, "scope_complete": False,
            "official_source_status": "assistant_aligned" if official else "pending_alignment",
            "official_reference": official,
            "annotation_source": "assistant_prd_draft_not_gold",
            "evidence_candidates": evidence})
    # 共享锚点片段的题归为同组；只是保守隔离预检，不替代完整相关集合审查。
    groups = []
    for q in questions:
        ids = {c for e in q["evidence_candidates"] for c in e["anchor_chunk_ids"]}
        touching = [g for g in groups if ids & g["chunks"]]
        merged = {"queries": [q["query_id"]], "chunks": ids}
        for g in touching:
            merged["queries"].extend(g["queries"])
            merged["chunks"].update(g["chunks"])
            groups.remove(g)
        groups.append(merged)
    for index, group in enumerate(groups, 1):
        for q in questions:
            if q["query_id"] in group["queries"]:
                q["evidence_overlap_group"] = f"anchor_group_{index:02d}"
    manifest = {"status": "draft_not_released", "snapshot_id": sid,
        "snapshot_file_sha256": digest(raw), "spec_sha256": digest(spec_path.read_bytes()),
        "old_questions_sha256": digest(old_questions_path.read_bytes()),
        "old_labels_sha256": digest(old_labels_path.read_bytes()),
        "question_count": len(questions), "anchor_overlap_groups": len(groups),
        "retrieval_scope": "all_snapshot_documents", "corpus_size": len(chunks),
        "document_ids": list(documents), "chunk_ids": [c["chunk_id"] for c in chunks],
        "old_positive_anchor_overlap": 0,
        "full_relevance_labels_complete": False,
        "limitations": ["锚点不等于完整相关集合", "旧快照缺少删除线标记，须核对原件",
                        "同义规则与跨文档重复需语义复核", "固定已见语料上的新问题，非未见文档泛化"]}
    lines = ["# 新留出问题候选：只审核这份", "", "状态：助手草稿，尚未发布为金标。",
        f"共{len(questions)}题；拟检索全快照{len(chunks)}片段。先不调用模型、不看模式结果。",
        "", "请核对问题、答案要点和正文证据是否一致；有版本差异请注明。",
        "无需手填片段ID或指标。这里只列证据锚点，不表示其他片段已判为不相关。", ""]
    for q in questions:
        lines.extend([f"## {q['query_id']}｜{q['module']}", "", q["question"], "",
            f"答案草稿：{q['reference_answer']}", "", f"PRD：{q['document_id']}",
            f"规则族：{q['query_family']}；共享锚点组：{q['evidence_overlap_group']}", ""])
        for e in q["evidence_candidates"]:
            lines.extend([f"> {e['quote']}", "",
                f"来源：`{e['block_id']}`，区间[{e['start_char']}, {e['end_char']})", ""])
        if q["official_reference"]:
            lines.extend([f"官方核对：{q['official_reference']['url']}", ""])
        lines.extend(["审核：待确认（认可 / 修改 / 排除）", ""])
    output.mkdir(parents=True)
    (output / "questions.draft.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), "utf-8")
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    (output / "先看这里_候选审核.md").write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "spec", "old-questions", "old-labels", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manuals", type=Path)
    args = parser.parse_args()
    prepare(
        args.snapshot, args.spec, args.old_questions, args.old_labels, args.output, args.manuals,
    )
