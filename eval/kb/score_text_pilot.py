"""对已批准正文金标离线计分；不扩大证据范围、不调用模型。"""

import argparse
import hashlib
import json
from pathlib import Path

from tc_rag.application.kb.evaluate import score_retrieval
from tc_rag.application.kb.pilot_metrics import score_pilot, summarize_pilot


def score(bundle: Path, gold_path: Path, run: Path, output: Path) -> None:
    if output.exists():
        raise ValueError("评分输出已存在")
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    if not gold or any(q.get("review_status") != "reviewed"
                       or q.get("scope_complete") is not True
                       or not q.get("release_approval") for q in gold):
        raise ValueError("需要已批准且范围完整的金标")
    snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    members = {d["version_id"]: d for d in snapshot["snapshot"]["documents"]}
    labels = {q["query_id"]: q for q in gold}
    if len(labels) != len(gold):
        raise ValueError("重复金标题号")
    if any(q["snapshot_id"] != snapshot["snapshot"]["snapshot_id"] for q in gold):
        raise ValueError("金标快照不一致")
    rows = json.loads((run / "candidates.json").read_text(encoding="utf-8"))
    expected = {(q, mode) for q in labels for mode in ("vector", "keyword", "hybrid")}
    if {(r["query_id"], r["mode"]) for r in rows} != expected or len(rows) != len(expected):
        raise ValueError("运行结果缺题、重复或模式不完整")
    scores = []
    for row in rows:
        q = labels[row["query_id"]]
        if row["snapshot_id"] != q["snapshot_id"] or row["question"] != q["question"]:
            raise ValueError("运行与金标不一致")
        if sorted(row["document_ids"]) != sorted(q["document_ids"]):
            raise ValueError("查询范围与金标不一致")
        candidates = []
        for chunk in row["candidates"]:
            sources = []
            for source in chunk["sources"]:
                block = blocks[source["block_id"]]
                if block["origin"] == "text":
                    sources.append(dict(
                        source,
                        original_block_id=snapshot["original_block_ids"].get(
                            block["block_id"], block["block_id"]),
                        content_sha256=members[block["document_id"]]["content_sha256"],
                    ))
            candidates.append({"chunk_id": chunk["chunk_id"], "sources": sources})
        result = {"top_k": row["top_k"], "candidates": candidates}
        metric = score_pilot(result, q)
        metric.update(mode=row["mode"],
                      strict_recall=score_retrieval(result, q, overlap_threshold=1)["recall"])
        scores.append(metric)
    summary = {}
    for mode in ("vector", "keyword", "hybrid"):
        selected = [r for r in scores if r["mode"] == mode]
        summary[mode] = summarize_pilot(selected)
        for key in ("mrr", "all_evidence_found", "strict_recall"):
            summary[mode][key] = sum(r[key] for r in selected) / len(selected)
    output.mkdir(parents=True)
    payload = {"metric_scope": "approved_prd_direct_text_location",
               "metric_schema_version": "1.1",
               "metric_definitions": {"precision": "relevant_candidates/requested_k",
                                      "recall": "covered_fact_units/total_fact_units",
                                      "f1": "not_computed_mixed_units"},
               "k": sorted({r["k"] for r in scores}), "rows": scores, "summary": summary,
               "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
               "result_sha256": hashlib.sha256((run / "candidates.json").read_bytes()).hexdigest()}
    (output / "scores.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "gold", "run", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    score(args.bundle, args.gold, args.run, args.output)
