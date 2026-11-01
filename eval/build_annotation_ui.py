"""生成可双击打开的私有标注页；不读取密钥、不联网、不改写已有标签。"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_payload(labels_dir: Path, dataset: Path | None = None) -> dict[str, Any]:
    """保留人工候选审核与完整语料审核的区别，草稿不能自动成为人工金标。"""
    grouped = json.loads((labels_dir / "corpus_groups.json").read_text(encoding="utf-8"))
    labels = json.loads((labels_dir / "labels_by_group.json").read_text(encoding="utf-8"))
    groups = grouped["groups"]
    queries = labels["queries"]
    if not groups or not queries:
        raise ValueError("语料组和查询均不能为空")
    group_ids = [group["group_id"] for group in groups]
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("语料组 ID 重复")
    corpus_ids = [cid for group in groups for cid in group["corpus_ids"]]
    if len(set(corpus_ids)) != len(corpus_ids):
        raise ValueError("同一语料 ID 不能出现在多个组中")
    allowed = set(group_ids)
    record_ids = [query["record_id"] for query in queries]
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("查询记录 ID 重复")
    for query in queries:
        if not query.get("query_id") or not isinstance(query.get("text"), str):
            raise ValueError("查询缺少可核验的 ID 或文字")
        positive = set(query.get("relevant_groups") or [])
        negative = set(query.get("irrelevant_groups") or [])
        uncertain = set(query.get("uncertain_groups") or [])
        hints = set(query.get("hint_top10_groups") or [])
        additions = set(query.get("additional_review_groups") or [])
        if not (positive | negative | uncertain | hints | additions) <= allowed:
            raise ValueError("标注或检索提示引用了不存在的语料组")
        if positive & negative or positive & uncertain or negative & uncertain:
            raise ValueError("同一语料组不能同时标为相关、不相关或待定")
        if query.get("corpus_review_complete"):
            if positive | negative != allowed or uncertain:
                raise ValueError("完整语料审核必须逐组完成明确判断")
    origins: dict[str, dict[str, Any]] = {}
    if dataset is not None:
        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        if manifest["source_run_id"] != labels["source_run_id"]:
            raise ValueError("标注与语料包不属于同一次运行")
        corpus = json.loads((dataset / "corpus.json").read_text(encoding="utf-8"))
        origins = {item["corpus_id"]: item for item in corpus}
        if set(corpus_ids) != set(origins):
            raise ValueError("语料包与分组的语料 ID 集合不同")
    # 问题提示属于展示资料，不参与查询、标签版本和浏览器进度的身份计算。
    display_questions: dict[str, str] = {}
    questions_path = labels_dir / "query_questions.json"
    if questions_path.exists():
        question_data = json.loads(questions_path.read_text(encoding="utf-8"))
        if question_data["source_run_id"] != labels["source_run_id"]:
            raise ValueError("问题提示与标注不属于同一次运行")
        query_map = {query["record_id"]: query for query in queries}
        for item in question_data["questions"]:
            record_id = item["record_id"]
            question = item.get("question")
            if record_id not in query_map or record_id in display_questions:
                raise ValueError("问题提示引用了未知或重复的查询")
            if item["query_id"] != query_map[record_id]["query_id"]:
                raise ValueError("问题提示与实际检索查询不匹配")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("问题提示不能为空")
            display_questions[record_id] = question.strip()
    material = {
        "source_run_id": labels["source_run_id"],
        "groups": groups,
        "queries": [
            {key: query[key] for key in ("record_id", "query_id", "text", "stage")}
            for query in queries
        ],
    }
    return {
        "schema_version": "1.0",
        "dataset_id": _digest(material),
        "seed_revision": _digest(labels),
        "source_run_id": labels["source_run_id"],
        "display_questions": display_questions,
        "question_hints_fingerprint": _digest(display_questions),
        "groups": [
            {
                **group,
                "sources": [
                    {
                        "corpus_id": cid,
                        "origin": origins.get(cid, {}).get("origin", "unknown"),
                        "locator": origins.get(cid, {}).get("locator"),
                    }
                    for cid in group["corpus_ids"]
                ],
            }
            for group in groups
        ],
        "queries": queries,
    }


def render_page(payload: dict[str, Any], template: Path | None = None) -> str:
    """数据只作为 JSON 和纯文字呈现，原文中的 HTML/脚本不允许执行。"""
    # 默认模板与 Python 入口分开存放；显式传入的模板路径仍优先。
    source = (template or Path(__file__).parent / "templates" / "annotation_ui.html").read_text(
        encoding="utf-8"
    )
    marker = "__ANNOTATION_DATA__"
    if source.count(marker) != 1:
        raise ValueError("页面模板必须且只能包含一个数据占位符")
    encoded = json.dumps(payload, ensure_ascii=False)
    for old, new in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(old, new)
    return source.replace(marker, encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels-dir", type=Path, default=Path("workdir/dev/benchmarks/chapter6/labels")
    )
    parser.add_argument("--dataset", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("workdir/dev/benchmarks/chapter6/annotation/index.html"),
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出页面已存在，请使用新文件名，避免混淆正在标注的材料版本")
    payload = build_payload(args.labels_dir, args.dataset)
    page = render_page(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
