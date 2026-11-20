"""仅运行已冻结dev，多k复用同一次前20排名。"""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from tc_rag.domain.kb.binary_metrics import confusion, metrics


def run(bundle: Path, queries: Path, gold: Path, output: Path,
        keyword_algorithm: str = "tf") -> None:
    questions = json.loads((bundle / "questions.evidence_draft.json").read_text("utf-8"))
    if not questions or any(q.get("split") != "dev" for q in questions):
        raise ValueError("仅接受dev问题集，禁止留出集调参")
    labels = json.loads(gold.read_text("utf-8"))
    if any(r.get("review_status") != "reviewed" for r in labels):
        raise ValueError("金标尚未批准")
    expected = {q["query_id"] for q in questions}
    relevant = {}
    universes = {}
    for qid in expected:
        subset = [r for r in labels if r["query_id"] == qid]
        if len(subset) != 150 or len({r["chunk_id"] for r in subset}) != 150:
            raise ValueError("片段金标不完整")
        if any(type(r.get("label")) is not int or r["label"] not in (0, 1) for r in subset):
            raise ValueError("二元标签非法")
        universes[qid] = {r["chunk_id"] for r in subset}
        relevant[qid] = {r["chunk_id"] for r in subset if r["label"]}
    runner = runpy.run_path(str(Path(__file__).with_name("run_modes.py")))["run"]
    runner(bundle, queries, output, k=20, budget=40, keyword_algorithm=keyword_algorithm)
    candidates = json.loads((output / "candidates.json").read_text("utf-8"))
    results = []
    for row in candidates:
        qid = row["query_id"]
        for k in (1, 3, 5, 10, 20):
            ids = [c["chunk_id"] for c in row["candidates"][:k]]
            counts = confusion(universes[qid], relevant[qid], ids)
            first = next((i for i, key in enumerate(ids, 1) if key in relevant[qid]), None)
            results.append(dict(query_id=qid, mode=row["mode"], k=k, **counts,
                                **metrics(counts), rr=1 / first if first else 0,
                                returned_count=len(ids), missing_slots=k-len(ids)))
    summary = []
    for mode in ("vector", "keyword", "hybrid"):
        for k in (1, 3, 5, 10, 20):
            selected = [r for r in results if r["mode"] == mode and r["k"] == k]
            macro = {}
            valid_n = {}
            for key in ("precision", "recall", "f1", "accuracy", "rr"):
                values = [r[key] for r in selected if r[key] is not None]
                macro[key] = sum(values)/len(values) if values else None
                valid_n[key] = len(values)
            summary.append(dict(mode=mode, k=k, n=len(selected), macro=macro,
                                valid_n=valid_n, micro=metrics({
                                    key: sum(r[key] for r in selected)
                                    for key in ("tp", "fp", "fn", "tn")})))
    payload = {"status": "dev_only", "rows": results, "summary": summary,
               "gold_sha256": hashlib.sha256(gold.read_bytes()).hexdigest()}
    (output / "multik_scores.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 开发集多k基线", "", "固定每路预算40，最终k分别为1/3/5/10/20；仅dev，留出未运行。",
             "精确率分母为实际返回数；空结果precision=null并另报缺位，不能当作准确返回。",
             "", "| 模式 | k | 宏P | 宏R | 宏F1 | MRR@k |", "|---|---:|---:|---:|---:|---:|"]
    for r in summary:
        m = r["macro"]
        def fmt(value):
            return f"{value:.3f}" if value is not None else "未知"
        lines.append(f"| {r['mode']} | {r['k']} | " + " | ".join(
            fmt(m[key]) for key in ("precision", "recall", "f1", "rr")) + " |")
    (output / "先看这里_开发集多k.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "queries", "gold", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--keyword-algorithm", choices=("tf", "bm25"), default="tf")
    args = parser.parse_args()
    run(args.bundle, args.queries, args.gold, args.output, args.keyword_algorithm)
