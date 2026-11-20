"""固定dev候选ID，生成正文来源输入对照；不删除候选或调用模型。"""

import argparse
import hashlib
import json
from pathlib import Path


def prepare(pool_path: Path, snapshot_path: Path, output: Path) -> None:
    if output.exists():
        raise ValueError("输出已存在")
    original = pool_path.read_bytes()
    payload = json.loads(original)
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    if payload["snapshot_id"] != snapshot["snapshot"]["snapshot_id"]:
        raise ValueError("快照不匹配")
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    placeholder = Path("prompts/kb_rerank_text/v1.md").read_text("utf-8").strip()
    audit = []
    for question in payload["questions"]:
        if question["split"] != "dev":
            raise ValueError("禁止用已有留出集继续调输入")
        for candidate in question["candidates"]:
            pieces = []
            for source in candidate["sources"]:
                block = blocks[source["block_id"]]
                if block["origin"] != "text" or block["kind"] == "image":
                    continue
                start, end = source["start_char"], source["end_char"]
                if not 0 <= start < end <= len(block["content"]):
                    raise ValueError("来源区间非法")
                pieces.append(block["content"][start:end])
            text = "\n".join(pieces).strip()
            audit.append({"query_id": question["query_id"], "chunk_id": candidate["chunk_id"],
                          "original_chars": len(candidate["text"]), "text_chars": len(text),
                          "placeholder": not bool(text)})
            candidate["text"] = text or placeholder
    payload.update(input_policy="direct_text_projection_v1",
                   original_pool_sha256=hashlib.sha256(original).hexdigest(),
                   limitation="未去除旧快照丢失标记的删除线；占位符会改变输入，是来源策略对照")
    output.mkdir(parents=True)
    (output / "pool.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "projection_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pool", "snapshot", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    prepare(args.pool, args.snapshot, args.output)
