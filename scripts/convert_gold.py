"""转换 Markdown 中的 JSON 标注块，并校验标注内部引用。"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from tc_rag.domain import models


def validate_gold(data: dict[str, Any]) -> None:
    # 先校验每个对象的局部结构，再检查集合内 ID 和跨对象引用。
    types = {
        "evidence": models.EvidenceSpan,
        "features": models.Feature,
        "rules": models.RequirementRule,
        "test_points": models.TestPoint,
        "test_cases": models.TestCase,
        "issues": models.Issue,
    }
    id_fields = {
        "evidence": "evidence_id",
        "features": "feature_id",
        "rules": "rule_id",
        "test_points": "point_id",
        "test_cases": "case_id",
        "issues": "issue_id",
    }
    ids: dict[str, set[str]] = {}
    for key, model in types.items():
        rows = [model.model_validate(row) for row in data[key]]
        values = [getattr(row, id_fields[key]) for row in rows]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate IDs in {key}")
        ids[key] = set(values)

    def require(values: list[str], collection: str) -> None:
        if not set(values) <= ids[collection]:
            raise ValueError(f"Unknown reference in {collection}")

    for feature in data["features"]:
        require(feature["evidence_ids"], "evidence")
    for rule in data["rules"]:
        require([rule["feature_id"]], "features")
        require(rule["evidence_ids"], "evidence")
    for point in data["test_points"]:
        require(point["rule_ids"], "rules")
    points = {point["point_id"]: point for point in data["test_points"]}
    rules = {rule["rule_id"]: rule for rule in data["rules"]}
    for case in data["test_cases"]:
        require(case["point_ids"], "test_points")
        allowed_rules = {rid for pid in case["point_ids"] for rid in points[pid]["rule_ids"]}
        # 不仅检查 ID 是否存在，还限制步骤引用属于当前用例关联的测试点和规则。
        for step in case["steps"]:
            require(step["rule_ids"], "rules")
            require(step["evidence_ids"], "evidence")
            if not set(step["rule_ids"]) <= allowed_rules:
                raise ValueError("Step rules do not belong to case test points")
            allowed_evidence = {
                eid for rid in step["rule_ids"] for eid in rules[rid]["evidence_ids"]
            }
            if not set(step["evidence_ids"]) <= allowed_evidence:
                raise ValueError("Step evidence does not belong to its rules")
    all_ids = set().union(*ids.values())
    for issue in data["issues"]:
        if not set(issue["object_ids"]) <= all_ids:
            raise ValueError("Unknown issue object reference")


def convert(source: Path, target: Path) -> None:
    # 只提取人工维护的结构化代码块，不从自然语言 Markdown 猜测业务规则。
    blocks = re.findall(r"```json\s*\n(.*?)\n```", source.read_text(encoding="utf-8"), re.S)
    if len(blocks) != 1:
        raise ValueError("Expected exactly one JSON annotation block")
    data = json.loads(blocks[0])
    validate_gold(data)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    convert(args.source, args.target)
    print("Annotation models and references validated; human review still required.")
