"""离线诊断指定片段的完整排名，不改变正式候选预算。"""

import argparse
import json
from pathlib import Path

from tc_rag.infrastructure.retrieval.keyword import KeywordIndex, rrf
from tc_rag.infrastructure.retrieval.metrics import get_metric


def diagnose(bundle: Path, queries: Path, target: str) -> list[dict]:
    import numpy as np

    s = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
    qs = json.loads((bundle / "questions.evidence_draft.json").read_text(encoding="utf-8"))
    vectors = json.loads(queries.read_text(encoding="utf-8"))["queries"]
    by_query = {q["query_id"]: q for q in vectors}
    matrix = np.load(bundle / "document_vectors.npy", allow_pickle=False)
    chunks = s["chunks"]
    blocks = {b["block_id"]: b for b in s["blocks"]}
    members = {d["document_id"]: d["version_id"] for d in s["snapshot"]["documents"]}
    ids = [c["chunk_id"] for c in chunks]
    if target not in ids:
        raise ValueError("目标不在快照")
    result = []
    for q in qs:
        versions = {members[d] for d in q["document_ids"]}
        indexes = [i for i, c in enumerate(chunks) if c["sources"] and all(
            blocks[src["block_id"]]["document_id"] in versions for src in c["sources"])]
        allowed = {ids[i] for i in indexes}
        metric = get_metric(s["metric"])
        scores = metric.score(by_query[q["query_id"]]["vector"], matrix[indexes])
        order = np.argsort(-scores if metric.higher_is_better else scores, kind="stable")
        ranked = [ids[indexes[int(i)]] for i in order]
        keyword = KeywordIndex()
        keyword.build({ids[i]: chunks[i]["text"] for i in indexes})
        keys = keyword.search(q["question"], len(allowed), allowed)
        tokens = set(keyword.tokenizer(q["question"].lower()))
        counts = keyword.documents.get(target, {})
        matched = {t: counts[t] for t in sorted(tokens) if counts.get(t, 0)}
        fused = [key for key, _ in rrf([ranked[:20], keys[:20]], 20)]
        def rank(items):
            return items.index(target) + 1 if target in items else None
        result.append({
            "query_id": q["query_id"], "target_allowed": target in allowed,
            "allowed_count": len(allowed), "vector_rank": rank(ranked),
            "vector_score": float(scores[indexes.index(ids.index(target))])
            if target in allowed else None,
            "keyword_rank": rank(keys), "keyword_matched_terms": matched,
            "hybrid_rank_budget20": rank(fused),
            "vector_top5": [{"chunk_id": ids[indexes[int(i)]], "score": float(scores[i]),
                                 "preview": chunks[indexes[int(i)]]["text"][:100]}
                                for i in order[:5]],
        })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("输出已存在")
    rows = diagnose(args.bundle, args.queries, args.target)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2, allow_nan=False)
