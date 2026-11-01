"""只读准备 E2/E3/E5 材料；不装配模型、不调用网络、不覆盖已有材料包。"""

import argparse
import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from tc_rag.application.orchestration.pipeline import Pipeline
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.models import Chunk, DocumentBlock, EvidenceSpan


def query_candidates(stages: dict[str, Any], size: int) -> dict[str, str]:
    """按现有批次规则重建候选文字；必须以历史 query_id 核验后才能采用。"""
    if size <= 0:
        raise ValueError("批次大小必须为正")
    found: dict[str, str] = {}
    rules = stages["extract"]["rules"]
    for start in range(0, len(rules), size):
        text = stages["input"]["instruction"] + "\n" + "\n".join(
            rule["statement"] for rule in rules[start:start + size]
        )
        found[fingerprint(text)] = text
    points = [p for p in stages["plan_points"]["points"]
              if p["status"] == "ready" and p["applicability"] == "applicable"]

    def add(batch: list[dict[str, Any]]) -> None:
        text = "\n".join(p["objective"] for p in batch)
        found[fingerprint(text)] = text
        if len(batch) > 1:
            middle = len(batch) // 2
            add(batch[:middle])
            add(batch[middle:])

    for start in range(0, len(points), size):
        add(points[start:start + size])
    return found


def checked_vector(data: dict[str, Any], text: str, space: str, dimension: int) -> list[float]:
    values = data["vector"]
    if (data["space_id"] != space or data["text_fingerprint"] != fingerprint(text)
            or data["vector_fingerprint"] != fingerprint(values)
            or len(values) != dimension or not np.isfinite(values).all()):
        raise ValueError("向量缓存指纹、空间、维度或数值不匹配")
    return values


def prepare(database: Path, run_id: str, output: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError("输出目录已存在，请使用新目录，避免覆盖标注")
    # 只读连接 + 单个读事务，保证来源一致；不复制可能仍在写入的裸 SQLite 文件。
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")

        def read(table: str, key: str) -> dict[str, Any]:
            if table not in {"runs", "stage_snapshots", "index_manifests", "embedding_records",
                             "embedding_spaces"}:
                raise ValueError("不支持的数据表")
            row = connection.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
            if row is None:
                raise ValueError(f"缺少实验材料：{table} / {key}")
            return json.loads(row[0])

        run = read("runs", run_id)
        stages = {stage: read("stage_snapshots", f"{run_id}:{stage}")["payload"]
                  for stage in ("input", "chunk", "extract", "index", "retrieval", "plan_points")}
        manifest = read("index_manifests", stages["index"]["manifest_id"])["payload"]
        config = run["config_snapshot"]
        dimension = config["retrieval"]["embedding_dimension"]
        space = stages["index"]["space_id"]
        space_data = read("embedding_spaces", space)["payload"]
        if (
            fingerprint(space_data) != space
            or manifest["space_id"] != space
            or manifest["run_id"] != run_id
            or stages["index"]["index_version"] != manifest["version"]
            or dimension != space_data["dimension"]
            or config["models"]["embedding"]["model"] != space_data["model"]
            or config["retrieval"]["normalize"] != space_data["normalize"]
        ):
            raise ValueError("运行配置、历史索引与向量空间不一致，禁止混用材料")
        spans = Pipeline._retrieval_evidence(
            [EvidenceSpan.model_validate(e) for e in stages["extract"]["evidence"]],
            [DocumentBlock.model_validate(b) for b in stages["chunk"]["blocks"]],
            [Chunk.model_validate(c) for c in stages["chunk"]["chunks"]],
        )
        objects = {e.evidence_id: e.model_dump(mode="json") for e in spans}
        if (
            len(manifest["ids"]) != len(set(manifest["ids"]))
            or set(objects) != set(manifest["ids"])
        ):
            raise ValueError("当前重建语料与历史索引对象集合不一致，禁止猜测映射")

        def vector(text: str) -> list[float]:
            key = "embedding_" + fingerprint([space, text])
            return checked_vector(read("embedding_records", key)["payload"], text, space, dimension)

        corpus = [{"corpus_id": f"C{n:03d}", "object_id": key, **objects[key]}
                  for n, key in enumerate(manifest["ids"], 1)]
        # 同时保留缓存精度与索引实际 float32 精度，不能用后者冒充模型原始输出。
        cached_matrix = np.asarray(
            [vector(item["quote"]) for item in corpus], dtype=np.float64
        ).reshape(len(corpus), dimension)
        matrix = cached_matrix.astype(np.float32)
        if fingerprint([manifest["ids"], matrix.tolist(), space_data,
                        manifest["metric"]]) != manifest["version"]:
            raise ValueError("缓存重建矩阵与历史索引版本不一致，禁止混用更新后的向量")
        candidates = query_candidates(stages, config["generation"]["batch_point_limit"])
        queries, query_vectors, history = [], [], []
        for n, bundle in enumerate(stages["retrieval"]["bundles"], 1):
            if (
                bundle["space_id"] != space
                or bundle["index_version"] != manifest["version"]
                or bundle["scope"] != manifest["scope"]
            ):
                raise ValueError("历史检索混用了不同空间或索引版本，请分组准备")
            if not set(bundle["direct_ids"]) <= objects.keys():
                raise ValueError("历史检索的直接证据超出固定语料范围")
            text = candidates.get(bundle["query_id"])
            if text is None:
                raise ValueError("历史查询文字无法以哈希核验重建；请补材料，不得猜测")
            query_vectors.append(vector(text))
            record_id = f"Q{n:03d}"
            queries.append({"record_id": record_id, "query_id": bundle["query_id"],
                            "stage": bundle.get("generation", {}).get("stage"), "text": text,
                            "direct_ids": bundle["direct_ids"]})
            history.append({"record_id": record_id, **bundle})
        if not queries:
            raise ValueError("没有可用的历史检索查询，无法准备对照实验")

    output.mkdir(parents=True)

    def save(name: str, value: Any) -> None:
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save("corpus.json", corpus)
    save("queries.json", queries)
    save("rerank_history.json", history)
    save("labels.template.json", {"source_run_id": run_id, "judgment": "binary",
         "queries": [{**q, "relevant_corpus_ids": None, "reviewed_by": None, "notes": ""}
                     for q in queries]})
    np.save(output / "corpus_vectors.npy", matrix, allow_pickle=False)
    np.save(output / "corpus_vectors_cached.npy", cached_matrix, allow_pickle=False)
    np.save(output / "query_vectors.npy", np.asarray(query_vectors, dtype=np.float64),
            allow_pickle=False)
    (output / "prepare_retrieval.source.py").write_bytes(Path(__file__).read_bytes())
    sections = ["# 固定检索语料", "", "从全部语料判断相关性，不局限于历史 Top-k。", ""]
    for item in corpus:
        locator = json.dumps(item["locator"], ensure_ascii=False)
        sections += [f"## {item['corpus_id']}", f"对象 ID：{item['object_id']}",
                     f"来源：{item['origin']}；定位：{locator}",
                     "", item["quote"], ""]
    (output / "语料阅读.md").write_text("\n".join(sections), encoding="utf-8")
    instructions = ["# 从这里开始", "", "先复制 labels.template.json 为 labels.json，保留模板。",
                    "relevant_corpus_ids=null 表示未标注；填写如 [\"C003\", \"C008\"]。",
                    "必须浏览全部语料；仅主题相同而不能回答查询的片段不算相关。",
                    "明确无相关材料才填 []，这类无答案查询需另行报告，不直接喂给 rag evaluate。",
                    "本模板是检索实验工作表，不是 rag evaluate 的 gold.json。",
                    "先完成下面前三个查询；条件、例外是否相关不确定时在 notes 记录，勿猜测。",
                    "同源不同 ID 本轮按实际对象分别计数；不要只标其中一个来制造漏召回。", ""]
    for q in queries[:3]:
        instructions += [f"## {q['record_id']} / {q['stage']}", "", q["text"], ""]
    (output / "开始这里.md").write_text("\n".join(instructions), encoding="utf-8")
    summary = {
        "source_run_id": run_id, "source_status": run["status"],
        "document_fingerprint": stages["input"]["document_fingerprint"],
        "space_id": space, "index_version": manifest["version"], "dimension": dimension,
        "model": config["models"]["embedding"]["model"],
        "normalize": config["retrieval"]["normalize"],
        "vector_origin": "validated_embedding_cache; separate cached float64 and index float32",
        "corpus_vectors_dtype": "float32",
        "corpus_vectors_cached_dtype": "float64",
        "raw_vector_experiment_available": not config["retrieval"]["normalize"],
        "corpus_count": len(corpus), "query_count": len(queries),
        "unique_query_count": len({q["query_id"] for q in queries}),
        "faiss_available": importlib.util.find_spec("faiss") is not None,
        "annotation_status": "not_started", "network_requests": 0,
        "preparation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "files_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(output.iterdir()) if p.is_file()},
    }
    save("manifest.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("workdir/data.sqlite"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = prepare(args.database, args.run_id, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
