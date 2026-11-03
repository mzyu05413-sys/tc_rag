"""只读历史视觉质量诊断，不调用模型、不重新生成测试点。"""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tc_rag.core.config import load_config
from tc_rag.domain.models import Asset
from tc_rag.infrastructure.runtime.tokens import token_counter
from tc_rag.infrastructure.storage.files import LocalArtifactStore


def collect(database: Path, root: Path, run_id: str, counter_name: str, target: int) -> dict:
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        snapshots = {
            item["stage"]: item["payload"] for item in (
                json.loads(row[0]) for row in connection.execute(
                    "SELECT payload FROM stage_snapshots WHERE run_id=?", (run_id,)
                )
            )
        }
        chunk = snapshots.get("chunk")
        if chunk is None:
            raise ValueError("缺少 chunk 快照")
        blocks = chunk["blocks"]
        vision = [block for block in blocks if block.get("origin") == "vision"]
        image_types = {}
        for block in vision:
            if "_vision_" not in block["block_id"]:
                continue
            key = "vision_" + block["block_id"].rsplit("_vision_", 1)[1]
            row = connection.execute(
                "SELECT payload FROM rag_records WHERE id=?", (key,)
            ).fetchone()
            if row:
                details = json.loads(row[0])["payload"].get("details") or {}
                image_types[block["block_id"]] = details.get("description", {}).get(
                    "image_type", "unknown"
                )
        issues = {}
        for payload in (chunk, snapshots.get("issues", {}), snapshots.get("cases", {})):
            for issue in payload.get("items", payload.get("issues", [])):
                if "issue_id" in issue:
                    issues[issue["issue_id"]] = issue
        filtered = [item for item in issues.values()
                    if "测试点仅有图片证据，未生成测试点" in item["description"]]
        # aggregate 删除了对应点，历史载荷通常没有点→规则映射；不能据功能名猜图类型。
        saved_points = {point["point_id"]: point for point in snapshots.get(
            "plan_points", {}
        ).get("points", [])}
        extraction = snapshots.get("extract", {})
        rules = {rule["rule_id"]: rule for rule in extraction.get("rules", [])}
        evidence = {item["evidence_id"]: item for item in extraction.get("evidence", [])}
        type_counts = Counter()
        for item in filtered:
            types = set()
            for point_id in item["object_ids"]:
                for rule_id in saved_points.get(point_id, {}).get("rule_ids", []):
                    for evidence_id in rules.get(rule_id, {}).get("evidence_ids", []):
                        block_id = evidence.get(evidence_id, {}).get("block_id")
                        if block_id in image_types:
                            types.add(image_types[block_id])
            type_counts["+".join(sorted(types)) or "unknown"] += 1
        counter = None
        counter_error = None
        try:
            # tiktoken 缓存缺失时禁止下载编码表；如实报告 token 未测量。
            with patch("tiktoken.load.read_file", side_effect=OSError("offline cache unavailable")):
                counter = token_counter(counter_name)
        except (OSError, ValueError, ImportError):
            counter_error = "本地编码表不可用，未联网下载；token 统计留空"
        volumes = [{
            "block_id": block["block_id"], "characters": len(block["content"]),
            "tokens": counter(block["content"]) if counter else None,
            "image_type": image_types.get(block["block_id"], "unknown"),
        } for block in vision]
        source_asset = None
        asset_id = chunk.get("document", {}).get("asset_id")
        if asset_id:
            row = connection.execute(
                "SELECT payload FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
            if row:
                source_asset = Asset.model_validate_json(row[0])
    pdf = {"status": "not_pdf", "pages": []}
    if source_asset and source_asset.relative_path.lower().endswith(".pdf"):
        import pymupdf

        content = LocalArtifactStore(root).read(source_asset)
        with pymupdf.open(stream=content, filetype="pdf") as document:
            pages = [{"page": index + 1, "drawing_paths": len(page.get_drawings())}
                     for index, page in enumerate(document)]
        pdf = {"status": "measured", "pages": pages,
               "dense_threshold": 20, "dense_pages": sum(p["drawing_paths"] >= 20 for p in pages),
               "warning": "路径>=20只是密集绘图诊断，不等于漏掉流程图，也没有执行矢量图提取。"}
    return {
        "run_id": run_id, "model_calls": 0,
        "vision_only_aggregate_issues": len(filtered),
        "suppressed_point_references": sum(len(item["object_ids"]) for item in filtered),
        "aggregate_issues_by_image_type": dict(type_counts),
        "image_type_warning": (
            "历史无结构化类型或被删测试点缺少引用链时记 unknown，不按章节名猜测。"
        ),
        "vision_block_count": len(vision), "block_volumes": volumes,
        "counter": counter_name, "counter_error": counter_error, "chunk_target": target,
        "over_target_blocks": sum(row["tokens"] > target for row in volumes
                                  if row["tokens"] is not None),
        "unsupported_shape_issues": sum("含文本框或嵌入对象" in item["description"]
                                        for item in issues.values()),
        "pdf_drawings": pdf,
        "decision": "本轮不改变 aggregate、派生 Block 粒度或图形提取；先人工评估统计与来源。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=Path("workdir/local.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    result = collect(Path(config.storage.database), Path(config.project.workdir), args.run_id,
                     config.chunking.token_counter, config.chunking.target_tokens)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
