"""离线盘点重排打分分布和候选来源段落；不改排名或计算新模型分数。"""

import argparse
import hashlib
import json
import re
import runpy
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from tc_rag.core.config import load_config

stable_ties = runpy.run_path(str(Path(__file__).with_name("audit_rerank_ties.py")))["stable_ties"]


def profile(candidate, blocks):
    counts = Counter()
    for source in candidate["sources"]:
        block = blocks[source["block_id"]]
        start, end = source["start_char"], source["end_char"]
        text = block["content"]
        if not 0 <= start < end <= len(text):
            raise ValueError("来源区间非法")
        if block["origin"] != "vision":
            counts["body"] += end-start
            continue
        boundaries = list(re.finditer(r"【[^】\n]+】", text))
        segments = [(0, boundaries[0].start(), "vision_metadata")] if boundaries else [
            (0, len(text), "vision_unclassified")]
        for i, marker in enumerate(boundaries):
            header = marker.group()
            if "正文关联" in header:
                kind = "vision_context_restatement"
            elif "无法辨认" in header or "歧义" in header:
                kind = "vision_uncertainty"
            elif "图片可见事实" in header:
                kind = "vision_visible_facts"
            elif "图片明确表达" in header:
                kind = "vision_visible_relationships"
            elif "图片表格" in header:
                kind = "vision_table"
            else:
                kind = "vision_unclassified"
            segments.append((marker.start(), boundaries[i+1].start()
                             if i+1 < len(boundaries) else len(text), kind))
        for left, right, kind in segments:
            counts[kind] += max(0, min(end, right)-max(start, left))
    return dict(source_char_counts=dict(counts), source_char_total=sum(counts.values()),
                candidate_chars=len(candidate["text"]),
                note="source characters, including headers; not tokens or quality judgments")


def audit(plan_path, runs, snapshot_path, output, config=None):
    if output.exists():
        raise ValueError("输出已存在")
    def read(path):
        return json.loads(path.read_text("utf-8"))
    plan, snapshot = read(plan_path), read(snapshot_path)
    if plan["snapshot_id"] != snapshot["snapshot"]["snapshot_id"]:
        raise ValueError("快照不符")
    for path, digest in plan["source_hashes"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("来源指纹变化")
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    details, files = [], [plan_path, snapshot_path]
    for job in plan["jobs"]:
        pool_path = Path(job["pool_path"])
        if hashlib.sha256(pool_path.read_bytes()).hexdigest() != job["pool_sha256"]:
            raise ValueError("候选池变化")
        pools = {q["query_id"]: q for q in read(pool_path)["questions"]}
        gold_path = Path(job["gold_directory"]) / "labels.reviewed.json"
        labels = {(r["query_id"], r["chunk_id"]): r for r in read(gold_path)}
        results = runs / job["job_id"] / "results.jsonl"
        files.extend([pool_path, gold_path, results])
        rows = [json.loads(line) for line in results.read_text("utf-8").splitlines() if line]
        if len(rows) != len(pools) or {r["query_id"] for r in rows} != set(pools):
            raise ValueError("结果缺题或重复")
        for row in rows:
            if row["status"] != "success":
                details.append(dict(job_id=job["job_id"], query_id=row["query_id"],
                                    status="service_error_excluded"))
                continue
            tid = row["query_id"]
            candidates = {c["chunk_id"]: c for c in pools[tid]["candidates"]}
            if (row["pool_sha256"] != job["pool_sha256"]
                    or row["before"] != list(candidates) or stable_ties(row) != row["after"]):
                raise ValueError("排名、指纹或同分处理不符")
            scores = row["scores"]
            top = max(scores.values())
            records = []
            for cid in row["after"]:
                label = labels[(tid, cid)]
                records.append(dict(chunk_id=cid, chunk_order=label["chunk_order"],
                                    relevant=label["label"], score=scores[cid],
                                    before=row["before"].index(cid)+1,
                                    after=row["after"].index(cid)+1,
                                    text=candidates[cid]["text"],
                                    **profile(candidates[cid], blocks)))
            details.append(dict(job_id=job["job_id"], query_id=tid, status="audited",
                                question=pools[tid]["question"], candidates=records,
                                min_score=min(scores.values()), max_score=top,
                                distinct_scores=len(set(scores.values())),
                                highest_score_count=sum(v == top for v in scores.values()),
                                highest_score_relevant=sum(r["relevant"] for r in records
                                                           if r["score"] == top),
                                stable_ties_matches=True))
    metadata = dict(historical_config_model=plan["expected_model"],
                    actual_server_model_version="unknown",
                    raw_response_saved_in_experiment=False)
    if config is not None:
        endpoint = load_config(config).models.rerank
        url = urlsplit(endpoint.base_url or "")
        metadata["current_config_safe_fields"] = dict(model=endpoint.model,
            adapter=endpoint.adapter, enabled=endpoint.enabled, https=url.scheme == "https",
            path_ends_rerank=url.path.endswith("/rerank"))
        metadata["current_config_is_historical_proof"] = False
    files.append(Path("src/tc_rag/infrastructure/providers/rerank.py"))
    payload = dict(status="offline_audit_no_ranking_change", rows=details,
                   provider_metadata=metadata, model_calls=0,
                   hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                   limits=["高分不等于已校准概率，分数集中不能证明服务错误",
                           "字符占比与长度仅是输入描述，不能证明因果",
                           "可见事实段仍需人工审核，不自动变成完整规则金标"])
    output.mkdir(parents=True)
    (output / "audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "runs", "snapshot", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    audit(args.plan, args.runs, args.snapshot, args.output, args.config)
