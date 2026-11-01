"""把按组标注（G###）展开成按语料 ID 标注（C###），供 E2/E5 计算指标。

同一组内的 C### 原文完全相同，标注一组等于标注全部成员，避免"检索命中了同义副本
却因标注只写了其中一个而判为未命中"。
"""

import argparse
import json
from pathlib import Path
from typing import Any


def expand(
    groups: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """返回展开后的标注条目与统计；未知组号直接报错，不静默忽略。"""
    members = {group["group_id"]: list(group["corpus_ids"]) for group in groups}
    expanded: list[dict[str, Any]] = []
    labelled = 0
    empty = 0
    unlabelled = 0
    for entry in entries:
        relevant = entry.get("relevant_groups")
        if relevant is None:
            unlabelled += 1
            corpus_ids = None
        else:
            unknown = [item for item in relevant if item not in members]
            if unknown:
                raise ValueError(f"{entry['record_id']} 引用了不存在的组：{unknown[:3]}")
            corpus_ids = sorted({cid for item in relevant for cid in members[item]})
            if corpus_ids:
                labelled += 1
            else:
                empty += 1
        expanded.append(
            {
                "record_id": entry["record_id"],
                "query_id": entry.get("query_id"),
                "stage": entry.get("stage"),
                "text": entry.get("text"),
                "direct_ids": entry.get("direct_ids", []),
                "relevant_corpus_ids": corpus_ids,
                "reviewed_by": entry.get("reviewed_by"),
                "notes": entry.get("notes", ""),
            }
        )
    return expanded, {"labelled": labelled, "empty": empty, "unlabelled": unlabelled}


def main() -> int:
    parser = argparse.ArgumentParser(description="展开按组标注为按语料 ID 标注")
    parser.add_argument("--labels-dir", type=Path, default=Path(
        "workdir/dev/benchmarks/chapter6/labels"
    ))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    groups_payload = json.loads((args.labels_dir / "corpus_groups.json").read_text("utf-8"))
    by_group = json.loads((args.labels_dir / "labels_by_group.json").read_text("utf-8"))
    expanded, stats = expand(groups_payload["groups"], by_group["queries"])
    output = args.output or (args.labels_dir / "labels.json")
    payload = {
        "source_run_id": by_group["source_run_id"],
        "judgment": "binary",
        "expanded_from": "labels_by_group.json",
        "stats": stats,
        "queries": expanded,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **stats}, ensure_ascii=False))
    if stats["unlabelled"]:
        print(f"提示：仍有 {stats['unlabelled']} 条查询未标注（null），它们不参与指标计算")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
