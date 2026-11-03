"""输入文件与图文解析范围的只读检查。"""

from typing import Any

from tc_rag.application.services.design import resolved_image_asset_ids
from tc_rag.application.services.quality._common import _FAIL, _PASS, Check, SnapshotMap
from tc_rag.application.services.quality._common import blocks as _blocks
from tc_rag.application.services.quality._common import check as _check
from tc_rag.application.services.quality._common import fail as _fail
from tc_rag.application.services.quality._common import issues as _issues
from tc_rag.application.services.quality._common import not_applicable as _na
from tc_rag.application.services.quality._common import passed as _pass
from tc_rag.application.services.quality._common import safe as _safe
from tc_rag.application.services.quality._common import warn as _warn


def _input_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    input_payload = snapshots.get("input")
    chunk_payload = snapshots.get("chunk")

    def fingerprint_check() -> Check:
        if input_payload is None or chunk_payload is None:
            return _na("Q-IN-01", "input", "缺少 input/chunk 快照")
        expected = input_payload.get("document_fingerprint")
        actual = (chunk_payload.get("document") or {}).get("content_hash")
        counts = {
            "has_run_id": int(bool(run.get("run_id"))),
            "has_config": int(bool(run.get("config_snapshot"))),
            "fingerprint_match": int(bool(expected) and expected == actual),
        }
        status = _PASS if all(counts.values()) else _FAIL
        return _check(
            "Q-IN-01",
            "input",
            "error" if status == _FAIL else "info",
            status,
            "运行、配置与文档指纹一致" if status == _PASS else "运行、配置或文档指纹不一致",
            counts=counts,
            object_ids=[str(run.get("run_id", ""))],
        )

    def instruction_check() -> Check:
        instruction = str((input_payload or {}).get("instruction", "")).strip()
        if input_payload is None:
            return _na("Q-IN-02", "input", "缺少 input 快照")
        if instruction:
            return _pass("Q-IN-02", "input", "生成指令非空")
        return _warn("Q-IN-02", "input", "生成指令为空")

    return [
        _safe("Q-IN-01", "input", schema_errors, fingerprint_check),
        _safe("Q-IN-02", "input", schema_errors, instruction_check),
    ]


def _parse_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    chunk_payload = snapshots.get("chunk")

    def pairing_check() -> Check:
        if chunk_payload is None:
            return _na("Q-PA-01", "parse", "缺少 chunk 快照")
        blocks = _blocks(chunk_payload)
        images = [block for block in blocks if block.kind == "image"]
        derived = [block for block in blocks if block.origin == "vision"]
        vision_enabled = bool(
            (run.get("config_snapshot") or {})
            .get("parsing", {})
            .get("vision_enabled", False)
        )
        issue_items = _issues(snapshots)
        skipped_images = {
            obj for item in [*issue_items, *chunk_payload.get("issues", [])]
            if item.get("severity") == "info" and item.get("description") == "图片过小，已跳过"
            for obj in item.get("object_ids", [])
        }
        unparsed = {
            obj
            for item in issue_items
            if item.get("kind") == "unparsed"
            for obj in item.get("object_ids", [])
        }
        resolved_assets = resolved_image_asset_ids(blocks)
        stale_issues = [
            item
            for item in issue_items
            if item.get("kind") == "unparsed"
            and (
                any(str(obj) in resolved_assets for obj in item.get("object_ids", []))
                or any(
                    asset_id in str(item.get("description", ""))
                    for asset_id in resolved_assets
                )
            )
        ]
        missing: list[str] = []
        duplicate: list[str] = []
        for image in images:
            if image.block_id in skipped_images:
                continue
            matches = [
                item for item in derived if item.block_id.startswith(image.block_id + "_")
            ]
            if len(matches) > 1:
                duplicate.append(image.block_id)
            elif not matches and vision_enabled:
                missing.append(image.block_id)
            elif not matches and image.block_id not in unparsed:
                missing.append(image.block_id)
        bad_provenance = [
            item.block_id
            for item in derived
            if not item.processing_model or not item.processing_version
        ]
        counts = {
            "images": len(images),
            "vision_blocks": len(derived),
            "skipped": sum(image.block_id in skipped_images for image in images),
            "missing": len(missing),
            "duplicate": len(duplicate),
            "bad_provenance": len(bad_provenance),
            "stale_unparsed": len(stale_issues),
        }
        object_ids = missing + duplicate + bad_provenance
        if missing or duplicate or bad_provenance:
            return _fail(
                "Q-PA-01",
                "parse",
                "图片与 vision Block 配对或来源信息不完整",
                counts=counts,
                object_ids=object_ids,
            )
        if stale_issues:
            return _fail(
                "Q-PA-01",
                "parse",
                "图片已有 vision 解释，但仍保留未解析问题",
                counts=counts,
                object_ids=[
                    str(item.get("issue_id", "")) for item in stale_issues
                ],
            )
        if not vision_enabled and derived:
            return _warn(
                "Q-PA-01",
                "parse",
                "当前配置未启用 vision，但快照中存在 vision Block",
                counts=counts,
                object_ids=[item.block_id for item in derived],
            )
        return _pass(
            "Q-PA-01",
            "parse",
            "图片与 vision Block 配对完整",
            counts=counts,
        )

    def order_check() -> Check:
        if chunk_payload is None:
            return _na("Q-PA-02", "parse", "缺少 chunk 快照")
        blocks = _blocks(chunk_payload)
        orders = [block.order for block in blocks]
        unparsed = [
            item for item in _issues(snapshots) if item.get("kind") == "unparsed"
        ]
        counts = {
            "blocks": len(blocks),
            "duplicate_orders": len(orders) - len(set(orders)),
            "unparsed_issues": len(unparsed),
        }
        if counts["duplicate_orders"]:
            return _fail(
                "Q-PA-02",
                "parse",
                "Block 顺序索引重复",
                counts=counts,
                object_ids=[item.get("issue_id", "") for item in unparsed],
            )
        if unparsed:
            return _warn(
                "Q-PA-02",
                "parse",
                "存在未解析范围，需人工确认",
                counts=counts,
                object_ids=sorted(
                    {str(item.get("issue_id", "")) for item in unparsed}
                ),
            )
        return _pass("Q-PA-02", "parse", "Block 顺序与解析问题检查通过", counts=counts)

    return [
        _safe("Q-PA-01", "parse", schema_errors, pairing_check),
        _safe("Q-PA-02", "parse", schema_errors, order_check),
    ]


# 内部跨模块别名；原定义保留，便于纯搬移 AST 核验。
input_checks = _input_checks
parse_checks = _parse_checks
