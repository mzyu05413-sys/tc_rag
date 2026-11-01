"""人工候选标注的校验和历史重排评分；未知判断始终保留为未知。"""

import copy
from collections import Counter
from math import log2
from typing import Any

from experiment_metrics import percentiles

DECISIONS = {"relevant", "irrelevant", "unsure"}


def _same_ids(actual: Any, expected: set[str]) -> bool:
    return isinstance(actual, list) and len(actual) == len(expected) and set(actual) == expected


def validate_review(review: dict[str, Any], seed: dict[str, Any]) -> None:
    """同时核对查询身份和冗余导出字段，防止导入错材料或伪造全库完成。"""
    if review.get("qa_simulation") or review.get("tool") != "tc-rag-annotation":
        raise ValueError("不是可导入的用户标注文件")
    for key in ("schema_version", "source_run_id", "dataset_id", "seed_revision"):
        if review.get(key) != seed[key]:
            raise ValueError(f"标注材料版本不匹配：{key}")
    members = {g["group_id"]: set(g["corpus_ids"]) for g in seed["groups"]}
    seeds = {q["record_id"]: q for q in seed["queries"]}
    entries = review["queries"]
    if not _same_ids([q["record_id"] for q in entries], set(seeds)):
        raise ValueError("查询记录集合缺失、未知或重复")
    confirmed = complete = 0
    for entry in entries:
        original = seeds[entry["record_id"]]
        for key in ("query_id", "stage", "text", "direct_ids"):
            if entry.get(key, [] if key == "direct_ids" else None) != original.get(key):
                raise ValueError(f"查询原文或身份不匹配：{entry['record_id']} / {key}")
        decisions = entry["judgments"]
        if not isinstance(decisions, dict) or not set(decisions) <= set(members):
            raise ValueError("判断引用了未知语料组")
        if any(value not in DECISIONS for value in decisions.values()):
            raise ValueError("未知的相关性判断")
        if type(entry.get("confirmed")) is not bool:
            raise ValueError("候选确认状态必须为布尔值")
        if type(entry.get("corpus_review_complete")) is not bool:
            raise ValueError("全库确认状态必须为布尔值")
        is_confirmed = entry["confirmed"]
        is_complete = entry["corpus_review_complete"]
        if is_complete and (
            not is_confirmed or set(decisions) != set(members) or "unsure" in decisions.values()
        ):
            raise ValueError("全库审核尚未逐组明确完成")
        if entry["review_scope"] != ("full_corpus" if is_complete else "presented_candidates"):
            raise ValueError("审核范围与完整性标记不一致")
        for state, field in (
            ("relevant", "relevant_groups"),
            ("irrelevant", "irrelevant_groups"),
            ("unsure", "uncertain_groups"),
        ):
            selected = {gid for gid, value in decisions.items() if value == state}
            if state == "relevant" and not is_confirmed:
                if entry[field] is not None or entry["relevant_corpus_ids"] is not None:
                    raise ValueError("未确认的建议不能导入为人工正例")
                continue
            if not _same_ids(entry[field], selected):
                raise ValueError(f"判断与导出组列表不一致：{field}")
            if state != "unsure":
                cids = {cid for gid in selected for cid in members[gid]}
                if not _same_ids(entry[field.replace("groups", "corpus_ids")], cids):
                    raise ValueError("语料 ID 展开结果与组判断不一致")
        if not _same_ids(entry["unreviewed_groups"], set(members) - set(decisions)):
            raise ValueError("未判断列表不一致")
        if not isinstance(entry.get("notes"), str):
            raise ValueError("人工备注必须为文字")
        if entry.get("original_notes", "") != original.get("notes", ""):
            raise ValueError("原始备注不匹配")
        confirmed += is_confirmed
        complete += is_complete
    if review["progress"] != {
        "candidate_confirmed": confirmed, "corpus_complete": complete, "total": len(entries)
    }:
        raise ValueError("进度统计与逐条标注不一致")


def normalize_review(
    review: dict[str, Any], seed: dict[str, Any], corrections: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """保留候选审核范围；聊天补充必须明确指定旧值、新值和来源。"""
    validate_review(review, seed)
    seeds = {q["record_id"]: q for q in seed["queries"]}
    members = {g["group_id"]: g["corpus_ids"] for g in seed["groups"]}
    entries = copy.deepcopy(review["queries"])
    by_id = {q["record_id"]: q for q in entries}
    for correction in corrections:
        entry = by_id[correction["record_id"]]
        gid = correction["group_id"]
        if gid not in members or correction["value"] not in DECISIONS:
            raise ValueError("补充确认的语料组或判断不存在")
        if entry["judgments"].get(gid) != correction["expected"]:
            raise ValueError("补充确认的旧值不匹配")
        if not correction.get("note") or correction.get("source") != "user-via-chat":
            raise ValueError("补充确认必须记录用户来源与说明")
        entry["judgments"][gid] = correction["value"]
    grouped, expanded = [], []
    for entry in entries:
        original = seeds[entry["record_id"]]
        judgments = entry["judgments"]
        item = copy.deepcopy(original)
        item.update({key: entry[key] for key in (
            "confirmed", "confirmed_at", "reviewed_by", "review_scope", "corpus_review_complete"
        )})
        item["judgments"] = judgments
        item["original_notes"] = entry.get("original_notes", "")
        item["review_notes"] = entry["notes"]
        item["notes"] = "\n".join(filter(None, [item["original_notes"], entry["notes"]]))
        item["chat_corrections"] = [
            c for c in corrections if c["record_id"] == entry["record_id"]
        ]
        for c in item["chat_corrections"]:
            item["notes"] += f"\n用户补充确认：{c['group_id']} → {c['value']}；{c['note']}"
        for state, field in (
            ("relevant", "relevant_groups"),
            ("irrelevant", "irrelevant_groups"),
            ("unsure", "uncertain_groups"),
        ):
            item[field] = sorted(g for g, value in judgments.items() if value == state)
        if not entry["confirmed"]:
            item["relevant_groups"] = None
        item["unreviewed_groups"] = sorted(set(members) - set(judgments))
        grouped.append(item)
        corpus_item = copy.deepcopy(item)
        for field in (
            "relevant_groups", "irrelevant_groups", "uncertain_groups", "unreviewed_groups"
        ):
            values = item[field]
            corpus_item[field.replace("groups", "corpus_ids")] = (
                sorted({cid for gid in values for cid in members[gid]})
                if values is not None else None
            )
        expanded.append(corpus_item)
    return grouped, expanded


def strict_metrics(
    ranked: list[str], positive: set[str], negative: set[str], complete: bool = False
) -> dict[str, float | None]:
    """仅返回能确定的分数：未知项不能按负例计分，且单列全库 Recall。"""
    if positive & negative or len(ranked) != len(set(ranked)):
        raise ValueError("标注冲突或排名含重复对象")
    known = positive | negative
    if complete and not set(ranked) <= known:
        raise ValueError("全库完成标记与未知排名冲突")
    top5, top10 = ranked[:5], ranked[:10]
    hit: float | None = 1.0 if set(top5) & positive else (
        0.0 if set(top5) <= negative else None
    )
    mrr: float | None = 0.0
    for rank, oid in enumerate(top10, 1):
        if oid not in known:
            mrr = None
            break
        if oid in positive:
            mrr = 1.0 / rank
            break
    ndcg = None
    # 正例已知至少五条时，IDCG@5 已确定，无需假装全库审核完成。
    if set(top5) <= known and (len(positive) >= 5 or complete):
        ideal = sum(1.0 / log2(i + 1) for i in range(1, min(5, len(positive)) + 1))
        gain = sum(1.0 / log2(i + 1) for i, oid in enumerate(top5, 1) if oid in positive)
        ndcg = gain / ideal if ideal else None
    return {
        "recall@5": len(set(top5) & positive) / len(positive) if complete and positive else None,
        "hit@5": hit, "mrr@10": mrr, "ndcg@5": ndcg,
    }


def evaluate_history(
    entries: list[dict[str, Any]], corpus: list[dict[str, Any]], history: list[dict[str, Any]]
) -> dict[str, Any]:
    """仅比较同一批查询的同一候选集合，按指标各自取双侧可确定的配对子集。"""
    alias = {c["object_id"]: c["corpus_id"] for c in corpus}
    records = {h["record_id"]: h for h in history}
    if len(alias) != len(corpus) or len(records) != len(history):
        raise ValueError("语料或历史查询记录重复")
    statuses: Counter[str] = Counter()
    rows, lengths, top_ns, latencies = [], set(), set(), []
    for entry in entries:
        record = records[entry["record_id"]]
        if record["query_id"] != entry["query_id"] or record["direct_ids"] != entry["direct_ids"]:
            raise ValueError("历史查询身份不匹配")
        rankings = {}
        for side, field in (("before", "vector_hits"), ("after", "hits")):
            hits = sorted(record[field], key=lambda hit: hit["rank"])
            if [h["rank"] for h in hits] != list(range(1, len(hits) + 1)):
                raise ValueError("历史排名不连续")
            rankings[side] = [alias[h["object_id"]] for h in hits]
        if set(rankings["before"]) != set(rankings["after"]):
            raise ValueError("重排前后候选集发生变化，不能作固定候选对照")
        positive = set(entry["relevant_corpus_ids"] or [])
        negative = set(entry["irrelevant_corpus_ids"])
        known = positive | negative
        row = {"record_id": entry["record_id"], "stage": entry["stage"], "rankings": rankings}
        for side, ranked in rankings.items():
            row[side] = strict_metrics(ranked, positive, negative, entry["corpus_review_complete"])
            if not entry["confirmed"]:
                row[side] = {key: None for key in row[side]}
        row["unknown_top5"] = {
            side: [cid for cid in ranked[:5] if cid not in known]
            for side, ranked in rankings.items()
        }
        rows.append(row)
        lengths.add(len(rankings["before"]))
        status = record["rerank"]["status"]
        statuses[status] += 1
        if status == "succeeded":
            top_ns.add(len(record["rerank"]["scores"]))
            latencies.append(record["timings_ms"]["rerank_ms"])
    aggregates = {}
    for scope in ("all", "plan_points", "generate"):
        selected = rows if scope == "all" else [r for r in rows if r["stage"] == scope]
        summary = {}
        for metric in ("recall@5", "hit@5", "mrr@10", "ndcg@5"):
            paired = [r for r in selected if r["before"][metric] is not None
                      and r["after"][metric] is not None]
            before = sum(r["before"][metric] for r in paired) / len(paired) if paired else None
            after = sum(r["after"][metric] for r in paired) / len(paired) if paired else None
            summary[metric] = {
                "before": before, "after": after,
                "delta": after - before if before is not None and after is not None else None,
                "paired_queries": [r["record_id"] for r in paired],
                "excluded_queries": [r["record_id"] for r in selected if r not in paired],
            }
        aggregates[scope] = summary
    return {
        "aggregation": "paired_exact_judgments", "query_count": len(rows),
        "full_corpus_review_count": sum(bool(e["corpus_review_complete"]) for e in entries),
        "candidate_counts": sorted(lengths), "rerank_top_n_observed": sorted(top_ns),
        "rerank_status_counts": dict(statuses),
        "historical_rerank_ms": {**percentiles(latencies), "samples": len(latencies)},
        "new_model_calls": 0, "aggregates": aggregates, "per_query": rows,
    }
