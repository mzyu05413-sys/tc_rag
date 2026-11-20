"""离线 R1：复用现有索引、关键词与 RRF，不读取密钥、不访问模型。"""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

import numpy as np

from tc_rag.application.ports.contracts import EmbeddingSpace
from tc_rag.domain.identity import fingerprint
from tc_rag.infrastructure.retrieval.index import NumpyIndex
from tc_rag.infrastructure.retrieval.keyword import KeywordIndex, rrf


def run(
    bundle: Path, queries: Path, output: Path, k: int = 5, budget: int = 20,
    filter_punctuation: bool = False,
    keyword_algorithm: str = "tf",
) -> None:
    """只输出候选；最终金标未冻结时不生成正式指标。"""
    if output.exists():
        raise ValueError("输出目录已存在，拒绝覆盖")
    if not 0 < k <= budget:
        raise ValueError("需要 0 < k <= budget")
    manifest = json.loads((bundle / "manifest.partial.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise ValueError("材料指纹不匹配：" + name)
    snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
    questions = json.loads((bundle / "questions.evidence_draft.json").read_text(encoding="utf-8"))
    supplied = json.loads(queries.read_text(encoding="utf-8"))
    space = EmbeddingSpace(**supplied["space"])
    if fingerprint(asdict(space)) != snapshot["snapshot"]["embedding_space_id"]:
        raise ValueError("查询向量空间不匹配")
    if supplied["snapshot_id"] != snapshot["snapshot"]["snapshot_id"]:
        raise ValueError("查询向量快照不匹配")
    records = supplied["queries"]
    if len({r["query_id"] for r in records}) != len(records):
        raise ValueError("查询 ID 重复")
    by_query = {r["query_id"]: r for r in records}
    for q in questions:
        record = by_query.get(q["query_id"])
        if record is None or record["question"] != q["question"]:
            raise ValueError("缺查询向量或问题文字不一致：" + q["query_id"])
        vector = np.asarray(record["vector"], dtype=np.float32)
        if vector.shape != (space.dimension,) or not np.isfinite(vector).all():
            raise ValueError("查询向量维度或数值非法")
    ids = json.loads((bundle / "chunk_ids.json").read_text(encoding="utf-8"))
    if ids != [c["chunk_id"] for c in snapshot["chunks"]]:
        raise ValueError("向量行序与快照不一致")
    matrix = np.load(bundle / "document_vectors.npy", allow_pickle=False)
    index = NumpyIndex(snapshot["metric"])
    index.build(ids, matrix.tolist(), space)
    if index.version != snapshot["index_version"]:
        raise ValueError("完整索引版本不一致")
    chunks = {c["chunk_id"]: c for c in snapshot["chunks"]}
    blocks = {b["block_id"]: b for b in snapshot["blocks"]}
    members = {d["document_id"]: d for d in snapshot["snapshot"]["documents"]}
    rows = []
    for q in questions:
        versions = {members[d]["version_id"] for d in q["document_ids"]}
        allowed = {key for key, c in chunks.items()
                   if c["sources"] and all(blocks[s["block_id"]]["document_id"] in versions
                                           for s in c["sources"])}
        if keyword_algorithm == "bm25":
            from tc_rag.infrastructure.retrieval.bm25 import BM25Index

            keyword = BM25Index()
        elif keyword_algorithm == "tf":
            keyword = KeywordIndex()
        else:
            raise ValueError("未知关键词算法")
        if filter_punctuation:
            # 实验适配：仍复用正式词频算法，只包装两侧共用的分词器。
            import runpy

            keep = runpy.run_path(str(Path(__file__).with_name("token_filters.py")))["keep_token"]
            original_tokenizer = keyword.tokenizer
            keyword.tokenizer = lambda text, original=original_tokenizer: [
                token for token in original(text) if keep(token)
            ]
        started = perf_counter()
        keyword.build({key: chunks[key]["text"] for key in ids if key in allowed})
        build_seconds = perf_counter() - started
        started = perf_counter()
        hits = index.search(by_query[q["query_id"]]["vector"], budget, allowed)
        vector_seconds = perf_counter() - started
        vector_ids = [h.object_id for h in hits]
        started = perf_counter()
        keyword_ids = keyword.search(q["question"], budget, allowed)
        keyword_seconds = perf_counter() - started
        started = perf_counter()
        fused = rrf([vector_ids, keyword_ids], budget)
        fusion_seconds = perf_counter() - started
        for mode, ranking in [("vector", vector_ids), ("keyword", keyword_ids),
                              ("hybrid", [key for key, _ in fused])]:
            rows.append({"query_id": q["query_id"], "question": q["question"],
                         "snapshot_id": snapshot["snapshot"]["snapshot_id"],
                         "document_ids": q["document_ids"], "mode": mode, "top_k": k,
                         "vector_ranking": vector_ids, "keyword_ranking": keyword_ids,
                         "fusion_scores": dict(fused),
                         "vector_scores": {h.object_id: h.raw_score for h in hits},
                         "timings": {"keyword_build": build_seconds,
                                     "vector_search": vector_seconds,
                                     "keyword_search": keyword_seconds, "fusion": fusion_seconds},
                         "candidates": [chunks[key] for key in ranking[:k]],
                         "formal_metrics": None})
    output.mkdir(parents=True)
    (output / "candidates.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (output / "complete.json").write_text(json.dumps({
        "status": "retrieval_only", "formal_gold_ready": False, "k": k,
        "budget": budget, "rrf_constant": 60, "rerank": False,
        "keyword_algorithm": keyword_algorithm,
        "bm25_parameters": {"k1": 1.2, "b": 0.75} if keyword_algorithm == "bm25" else None,
        "token_filter": "unicode_punctuation_v1" if filter_punctuation else "production_default",
        "query_file_sha256": hashlib.sha256(queries.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(
            (bundle / "manifest.partial.json").read_bytes()).hexdigest(),
    }), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--budget", type=int, default=20)
    parser.add_argument("--filter-punctuation", action="store_true")
    parser.add_argument("--keyword-algorithm", choices=("tf", "bm25"), default="tf")
    args = parser.parse_args()
    run(args.bundle, args.queries, args.output, args.k, args.budget,
        args.filter_punctuation, args.keyword_algorithm)
