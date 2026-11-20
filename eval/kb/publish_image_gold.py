"""按显式用户裁定发布图文片段金标；保留正文基线，不重跑检索。"""

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path


def read(path):
    return json.loads(path.read_text("utf-8"))


def publish(original: Path, audit: Path, decisions: Path, snapshot_path: Path, output: Path):
    if output.exists():
        raise ValueError("发布目录已存在")
    labels = read(original / "labels.reviewed.json")
    scope = read(original / "scope_manifest.json")
    evidence = read(audit / "evidence.prelabel.json")
    receipt = read(decisions)
    snapshot = read(snapshot_path)
    sid = snapshot["snapshot"]["snapshot_id"]
    if not (sid == scope["snapshot_id"] == evidence["snapshot_id"]):
        raise ValueError("快照不一致")
    for name, digest in evidence["hashes"].items():
        if hashlib.sha256((audit / name).read_bytes()).hexdigest() != digest:
            raise ValueError("审核输入发生变化")
    chunks = {c["chunk_id"]: c for c in snapshot["chunks"]}
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    by_label = {(r["query_id"], r["chunk_id"]): r for r in labels}
    if len(by_label) != len(labels) or not scope.get("gold_complete"):
        raise ValueError("正文金标不完整")
    for tid, ids in scope["question_scopes"].items():
        subset = [r for r in labels if r["query_id"] == tid]
        if len(ids) != len(set(ids)) or {r["chunk_id"] for r in subset} != set(ids):
            raise ValueError("逐题全集不一致")
    for r in labels:
        if (r["review_status"] != "reviewed" or r["snapshot_id"] != sid
                or type(r["label"]) is not int or r["label"] not in (0, 1)
                or r["text_sha256"] != hashlib.sha256(
                    chunks[r["chunk_id"]]["text"].encode("utf-8")).hexdigest()):
            raise ValueError("正文标签非法或文本已改变")
        r["body_label"] = r["label"]
        r["image_evidence"] = []
    image_keys = {r["image_key"] for r in evidence["images"]}
    if not set(receipt["image_decisions"]) <= image_keys:
        raise ValueError("裁定了不存在的图片")
    adapted = receipt["prototype_adaptation"]
    reviewed_images = []
    for original_image in evidence["images"]:
        image = deepcopy(original_image)
        key = image["image_key"]
        label = receipt["image_decisions"].get(key, image["proposed_relevance"])
        if type(label) is not int or label not in (0, 1):
            raise ValueError("图片仍有未知状态")
        if hashlib.sha256(Path(image["copied_path"]).read_bytes()).hexdigest() != image[
                "image_sha256"]:
            raise ValueError("原图已改变")
        image.update(review_status="reviewed", reviewer="user", label=label)
        if label:
            block = blocks[image["vision_block_id"]]
            if block["origin"] != "vision" or block["content"] != image["vision_text"]:
                raise ValueError("图片解释与快照不同")
            text = block["content"]
            fact_start = text.index("【图片可见事实】")
            fact_end = text.find("【", fact_start + len("【图片可见事实】"))
            fact_end = len(text) if fact_end == -1 else fact_end
            fact_ids = adapted["fact_ids"] if key == adapted["image_key"] else image["fact_ids"]
            if key == adapted["image_key"]:
                image["prototype_adaptation"] = adapted
                image["rules"] = adapted["rules"]
            facts = []
            for fid in fact_ids:
                matches = list(re.finditer(r"(?m)^- " + re.escape(fid) + r"(?=\s).*$", text))
                matches = [m for m in matches if fact_start <= m.start() < m.end() <= fact_end]
                if len(matches) != 1:
                    raise ValueError("可见事实不唯一")
                match = matches[0]
                fact = dict(image_key=key, fact_id=fid, block_id=block["block_id"],
                            start_char=match.start(), end_char=match.end(), quote=match.group(),
                            coverage="partial_rule_support", rule_ids=image["rules"])
                covered = []
                for cid in scope["question_scopes"][image["task_id"]]:
                    if any(s["block_id"] == block["block_id"]
                           and s["start_char"] <= match.start()
                           and s["end_char"] >= match.end() for s in chunks[cid]["sources"]):
                        row = by_label[(image["task_id"], cid)]
                        row["image_evidence"].append(fact)
                        row["label"] = 1
                        covered.append(cid)
                if not covered:
                    raise ValueError("认可事实未完整落入任何片段，需单独处理跨片段覆盖")
                facts.append({**fact, "complete_fact_chunk_ids": covered})
            image["adopted_fact_spans"] = facts
        reviewed_images.append(image)
    for row in labels:
        row["annotation_policy"] = "body_or_verified_visible_image_subscenario"
        row["adoption_source"] = "image_review_conversation_20261118"
    scope.update(gold_version="expansion_multimodal_gold_v1",
                 evidence_policy="body_or_verified_visible_image_subscenario")
    files = [original / "labels.reviewed.json", original / "scope_manifest.json",
             original / "tasks.reviewed.json", audit / "evidence.prelabel.json", decisions,
             snapshot_path]
    receipt.update(snapshot_id=sid, files={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in files})
    summary = {"status": "published_user_adopted_multimodal_gold", "label_count": len(labels),
               "body_positive": sum(r["body_label"] for r in labels),
               "multimodal_positive": sum(r["label"] for r in labels),
               "image_count": len(reviewed_images),
               "approved_image_count": sum(r["label"] for r in reviewed_images),
               "formal_metrics": None, "split": "dev",
               "task_summaries": [{"task_id": tid,
                   "body_positive": sum(r["body_label"] for r in labels if r["query_id"] == tid),
                   "multimodal_positive": sum(r["label"] for r in labels if r["query_id"] == tid)}
                   for tid in scope["question_scopes"]]}
    output.mkdir(parents=True)
    for name, value in {"labels.reviewed.json": labels, "images.reviewed.json": reviewed_images,
                        "scope_manifest.json": scope, "publication_receipt.json": receipt,
                        "summary.json": summary,
                        "tasks.reviewed.json": read(original / "tasks.reviewed.json")}.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("original", "audit", "decisions", "snapshot", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    publish(args.original, args.audit, args.decisions, args.snapshot, args.output)
