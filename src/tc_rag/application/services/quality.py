"""只读质量检查：核对已保存快照的结构与引用，不判断业务语义。"""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from tc_rag.application.services.chunking import (
    cross_page_table_continuations,
    normalized_quote,
    verify_evidence,
)
from tc_rag.application.services.design import (
    boundary_values,
    case_signature,
    is_ui_display_point,
    is_ui_display_rule,
    qualitative_boundary_values,
    resolved_image_asset_ids,
    unsupported_numbers,
)
from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import Extraction
from tc_rag.domain.rag import ContextPayload
from tc_rag.domain.review import PointTree

Check = dict[str, Any]
SnapshotMap = Mapping[str, dict[str, Any]]

_FAIL = "fail"
_PASS = "pass"
_WARN = "warn"
_NA = "not_applicable"


def _check(
    check_id: str,
    stage: str,
    severity: str,
    status: str,
    summary: str,
    *,
    counts: dict[str, int] | None = None,
    object_ids: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> Check:
    return {
        "check_id": check_id,
        "stage": stage,
        "severity": severity,
        "status": status,
        "summary": summary,
        "counts": counts or {},
        "object_ids": (object_ids or [])[:50],
        "detail": detail or {},
    }


def _pass(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "info", _PASS, summary, **kwargs)


def _warn(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "warning", _WARN, summary, **kwargs)


def _fail(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "error", _FAIL, summary, **kwargs)


def _na(check_id: str, stage: str, summary: str, **kwargs: Any) -> Check:
    return _check(check_id, stage, "info", _NA, summary, **kwargs)


def _safe(
    check_id: str,
    stage: str,
    schema_errors: dict[str, str],
    operation: Callable[[], Check],
) -> Check:
    if stage in schema_errors:
        return _na(
            check_id,
            stage,
            "阶段快照结构无效，跳过该检查",
            detail={"schema_error": schema_errors[stage]},
        )
    try:
        return operation()
    except Exception as exc:
        return _fail(
            check_id,
            stage,
            "检查执行失败",
            detail={"error_type": type(exc).__name__},
        )


def _validate_stage(stage: str, payload: dict[str, Any]) -> None:
    if stage == "chunk":
        m.Document.model_validate(payload["document"])
        for block in payload.get("blocks", []):
            m.DocumentBlock.model_validate(block)
        for chunk in payload.get("chunks", []):
            m.Chunk.model_validate(chunk)
        for item in payload.get("issues", []):
            m.Issue.model_validate(item)
    elif stage == "extract":
        Extraction.model_validate(payload)
    elif stage == "plan_points":
        for point in payload.get("points", []):
            m.TestPoint.model_validate(point)
    elif stage == "point_tree":
        PointTree.model_validate(payload)
    elif stage == "cases":
        for case in payload.get("cases", []):
            m.TestCase.model_validate(case)
        for point in payload.get("points", []):
            _case_point(point)
        for evidence in payload.get("evidence", []):
            m.EvidenceSpan.model_validate(evidence)
        for feature in payload.get("features", []):
            m.Feature.model_validate(feature)
        for rule in payload.get("rules", []):
            m.RequirementRule.model_validate(rule)
        for link in payload.get("coverage", []):
            m.CoverageLink.model_validate(link)
        for item in payload.get("issues", []):
            m.Issue.model_validate(item)
    elif stage == "retrieval":
        for bundle in payload.get("bundles", []):
            ContextPayload.model_validate(bundle)
    elif stage == "invocations":
        for item in payload.get("items", []):
            m.ModelInvocation.model_validate(item)


def _schema_errors(snapshots: SnapshotMap) -> dict[str, str]:
    errors: dict[str, str] = {}
    for stage in (
        "chunk",
        "extract",
        "plan_points",
        "point_tree",
        "cases",
        "retrieval",
        "invocations",
    ):
        payload = snapshots.get(stage)
        if payload is None:
            continue
        try:
            _validate_stage(stage, payload)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            errors[stage] = type(exc).__name__
    return errors


def _blocks(chunk: dict[str, Any] | None) -> list[m.DocumentBlock]:
    if not chunk:
        return []
    return [m.DocumentBlock.model_validate(item) for item in chunk.get("blocks", [])]


def _case_point(item: dict[str, Any]) -> m.TestPoint:
    # cases 快照的 point 额外带导出用 category 字段；它不是领域契约的一部分。
    return m.TestPoint.model_validate(
        {key: value for key, value in item.items() if key != "category"}
    )


def _issues(snapshots: SnapshotMap) -> list[dict[str, Any]]:
    issue_snapshot = snapshots.get("issues")
    if issue_snapshot is not None:
        return list(issue_snapshot.get("items", []))
    cases = snapshots.get("cases")
    return [] if cases is None else list(cases.get("issues", []))


def _evidence_by_id(extract: dict[str, Any] | None) -> dict[str, m.EvidenceSpan]:
    if not extract:
        return {}
    return {
        item.evidence_id: item
        for item in (m.EvidenceSpan.model_validate(e) for e in extract.get("evidence", []))
    }


def _rules_by_id(extract: dict[str, Any] | None) -> dict[str, m.RequirementRule]:
    if not extract:
        return {}
    return {
        item.rule_id: item
        for item in (m.RequirementRule.model_validate(r) for r in extract.get("rules", []))
    }


def _source_evidence_ids(chunk: dict[str, Any] | None) -> set[str]:
    if not chunk:
        return set()
    result: set[str] = set()
    for item in chunk.get("chunks", []):
        for source in item.get("sources", []):
            model = m.ChunkSource.model_validate(source)
            result.add("source_" + fingerprint(model.model_dump(mode="json")))
    return result


def _contains_api_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "api_key" and item not in (None, ""):
                return True
            if _contains_api_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_api_key(item) for item in value)
    return False


def _overall(checks: list[Check]) -> dict[str, Any]:
    counts = Counter(check["status"] for check in checks)
    statuses = {status: counts.get(status, 0) for status in (_PASS, _FAIL, _WARN, _NA)}
    executed = statuses[_PASS] + statuses[_FAIL] + statuses[_WARN]
    if statuses[_FAIL]:
        status = _FAIL
    elif statuses[_WARN]:
        status = _WARN
    elif executed:
        status = _PASS
    else:
        status = _NA
    return {"status": status, "counts": statuses, "executed": executed}


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


def _chunk_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    chunk_payload = snapshots.get("chunk")

    def source_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-01", "chunk", "缺少 chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        bad: list[str] = []
        for chunk in chunk_payload.get("chunks", []):
            if not chunk.get("sources"):
                bad.append(chunk.get("chunk_id", ""))
                continue
            for source in chunk["sources"]:
                block = blocks.get(source.get("block_id", ""))
                start, end = source.get("start_char"), source.get("end_char")
                if (
                    block is None
                    or block.kind == "image"
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not 0 <= start < end <= len(block.content)
                ):
                    bad.append(chunk.get("chunk_id", ""))
                    break
        if bad:
            return _fail(
                "Q-CH-01",
                "chunk",
                "分块来源 Block 缺失、区间越界或指向图片",
                counts={"chunks": len(chunk_payload.get("chunks", [])), "invalid": len(set(bad))},
                object_ids=sorted(set(bad)),
            )
        return _pass(
            "Q-CH-01",
            "chunk",
            "分块来源区间与 Block 有效",
            counts={"chunks": len(chunk_payload.get("chunks", []))},
        )

    def text_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-02", "chunk", "缺少 chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        mismatch: list[str] = []
        for chunk in chunk_payload.get("chunks", []):
            try:
                expected = "\n".join(
                    blocks[source["block_id"]].content[
                        source["start_char"] : source["end_char"]
                    ]
                    for source in chunk.get("sources", [])
                )
            except KeyError:
                mismatch.append(chunk.get("chunk_id", ""))
                continue
            if expected != chunk.get("text"):
                mismatch.append(chunk.get("chunk_id", ""))
        if mismatch:
            return _fail(
                "Q-CH-02",
                "chunk",
                "分块文本与来源切片拼接不一致",
                counts={"mismatch": len(mismatch)},
                object_ids=mismatch,
            )
        return _pass(
            "Q-CH-02",
            "chunk",
            "分块文本与来源切片逐字一致",
            counts={"chunks": len(chunk_payload.get("chunks", []))},
        )

    def coverage_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-03", "chunk", "缺少 chunk 快照")
        blocks = _blocks(chunk_payload)
        eligible = {
            block.block_id
            for block in blocks
            if block.kind != "image" and block.content.strip()
        }
        covered = {
            source.get("block_id")
            for chunk in chunk_payload.get("chunks", [])
            for source in chunk.get("sources", [])
        }
        missing = sorted(eligible - covered)
        if missing:
            return _warn(
                "Q-CH-03",
                "chunk",
                "存在未进入任何分块的非空文字块",
                counts={"eligible": len(eligible), "missing": len(missing)},
                object_ids=missing,
            )
        return _pass(
            "Q-CH-03",
            "chunk",
            "所有非空文字块均有分块映射",
            counts={"eligible": len(eligible)},
        )

    def budget_check() -> Check:
        if chunk_payload is None:
            return _na("Q-CH-04", "chunk", "缺少 chunk 快照")
        target = int(
            (run.get("config_snapshot") or {})
            .get("chunking", {})
            .get("target_tokens", 900)
        )
        chunks = chunk_payload.get("chunks", [])
        over = [
            chunk.get("chunk_id", "")
            for chunk in chunks
            if int(chunk.get("token_estimate", 0)) > target
        ]
        blocks = _blocks(chunk_payload)
        continuation_missing: list[str] = []
        for item in cross_page_table_continuations(blocks):
            chunks_for_block = [
                chunk
                for chunk in chunks
                if any(
                    source.get("block_id") == item.continuation_id
                    for source in chunk.get("sources", [])
                )
            ]
            if not chunks_for_block:
                continue
            if all(
                not any(
                    source.get("block_id") == item.root_id
                    and source.get("start_char") == item.header_start
                    and source.get("end_char") == item.header_end
                    for source in chunk.get("sources", [])
                )
                for chunk in chunks_for_block
            ):
                continuation_missing.append(item.continuation_id)
        counts = {
            "over_target": len(over),
            "table_header_missing": len(continuation_missing),
        }
        if continuation_missing:
            return _warn(
                "Q-CH-04",
                "chunk",
                "跨页续表片段可能缺少表头",
                counts=counts,
                object_ids=sorted(set(over + continuation_missing)),
            )
        if over:
            return _warn(
                "Q-CH-04",
                "chunk",
                "存在超出目标预算的分块，未截断原文",
                counts=counts,
                object_ids=over,
            )
        return _pass("Q-CH-04", "chunk", "分块预算与跨页续表检查通过", counts=counts)

    return [
        _safe("Q-CH-01", "chunk", schema_errors, source_check),
        _safe("Q-CH-02", "chunk", schema_errors, text_check),
        _safe("Q-CH-03", "chunk", schema_errors, coverage_check),
        _safe("Q-CH-04", "chunk", schema_errors, budget_check),
    ]


def _extract_checks(
    snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    extract_payload = snapshots.get("extract")
    chunk_payload = snapshots.get("chunk")

    def evidence_check() -> Check:
        if extract_payload is None or chunk_payload is None:
            return _na("Q-EX-01", "extract", "缺少 extract/chunk 快照")
        blocks = {block.block_id: block for block in _blocks(chunk_payload)}
        invalid: list[str] = []
        for item in extract_payload.get("evidence", []):
            evidence = m.EvidenceSpan.model_validate(item)
            block = blocks.get(evidence.block_id)
            if block is None or not verify_evidence(evidence, block):
                invalid.append(evidence.evidence_id)
        if invalid:
            return _fail(
                "Q-EX-01",
                "extract",
                "引文不在对应 Block 中或定位/来源不一致",
                counts={
                    "evidence": len(extract_payload.get("evidence", [])),
                    "invalid": len(invalid),
                },
                object_ids=invalid,
            )
        return _pass(
            "Q-EX-01",
            "extract",
            "所有引文均可回到 Block 核验",
            counts={"evidence": len(extract_payload.get("evidence", []))},
        )

    def closure_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-02", "extract", "缺少 extract 快照")
        evidence = set(_evidence_by_id(extract_payload))
        bad_features = [
            feature.get("feature_id", "")
            for feature in extract_payload.get("features", [])
            if not set(feature.get("evidence_ids", [])) <= evidence
        ]
        bad_entities = [
            entity.get("entity_id", "")
            for entity in extract_payload.get("entities", [])
            if not set(entity.get("evidence_ids", [])) <= evidence
        ]
        if bad_features or bad_entities:
            return _fail(
                "Q-EX-02",
                "extract",
                "功能或实体引用了不存在的证据",
                counts={"bad_features": len(bad_features), "bad_entities": len(bad_entities)},
                object_ids=bad_features + bad_entities,
            )
        return _pass(
            "Q-EX-02",
            "extract",
            "功能与实体证据引用闭包完整",
            counts={
                "features": len(extract_payload.get("features", [])),
                "entities": len(extract_payload.get("entities", [])),
            },
        )

    def rule_closure() -> Check:
        if extract_payload is None:
            return _na("Q-EX-03", "extract", "缺少 extract 快照")
        evidence = set(_evidence_by_id(extract_payload))
        features = {
            feature.get("feature_id") for feature in extract_payload.get("features", [])
        }
        bad: list[str] = []
        for rule in extract_payload.get("rules", []):
            if (
                rule.get("feature_id") not in features
                or not set(rule.get("evidence_ids", [])) <= evidence
            ):
                bad.append(rule.get("rule_id", ""))
        if bad:
            return _fail(
                "Q-EX-03",
                "extract",
                "规则引用了不存在的功能或证据",
                counts={"rules": len(extract_payload.get("rules", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-EX-03",
            "extract",
            "规则功能/证据引用闭包完整",
            counts={"rules": len(extract_payload.get("rules", []))},
        )

    def constraint_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-04", "extract", "缺少 extract 快照")
        evidence = _evidence_by_id(extract_payload)
        bad_boundary: list[str] = []
        bad_unit: list[str] = []
        bad_precision: list[str] = []
        for item in extract_payload.get("rules", []):
            rule = m.RequirementRule.model_validate(item)
            constraint = rule.structured_constraint
            if constraint is None:
                continue
            quote = normalized_quote(
                "\n".join(
                    evidence[e].quote for e in rule.evidence_ids if e in evidence
                )
            )
            if constraint.boundary is not None and normalized_quote(
                str(constraint.boundary)
            ) not in quote:
                bad_boundary.append(rule.rule_id)
            if constraint.unit and normalized_quote(constraint.unit) not in quote:
                bad_unit.append(rule.rule_id)
            if constraint.precision is not None:
                reliable = (
                    bool(re.search(r"整数|[个条项字符]", quote))
                    if constraint.precision == 0
                    else bool(re.search(rf"小数\s*{constraint.precision}\s*位", quote))
                )
                if not reliable:
                    bad_precision.append(rule.rule_id)
        if bad_boundary:
            return _fail(
                "Q-EX-04",
                "extract",
                "结构化边界数值未在引文中出现",
                counts={"bad_boundary": len(bad_boundary)},
                object_ids=bad_boundary,
            )
        if bad_unit or bad_precision:
            return _warn(
                "Q-EX-04",
                "extract",
                "结构化单位或精度缺少明确依据",
                counts={"bad_unit": len(bad_unit), "bad_precision": len(bad_precision)},
                object_ids=sorted(set(bad_unit + bad_precision)),
            )
        return _pass(
            "Q-EX-04",
            "extract",
            "结构化约束均有引文依据",
            counts={"rules": len(extract_payload.get("rules", []))},
        )

    def issue_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-05", "extract", "缺少 extract 快照")
        invalid = [
            item
            for item in extract_payload.get("issues", [])
            if item.get("kind") == "validation"
        ]
        if invalid:
            return _warn(
                "Q-EX-05",
                "extract",
                "提炼阶段存在引用/定位校验问题",
                counts={"validation_issues": len(invalid)},
                object_ids=sorted(
                    {
                        str(obj)
                        for item in invalid
                        for obj in item.get("object_ids", [])
                    }
                ),
            )
        return _pass("Q-EX-05", "extract", "提炼阶段无引用校验问题")

    def skipped_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-06", "extract", "缺少 extract 快照")
        skipped = [
            item
            for item in extract_payload.get("issues", [])
            if "已跳过" in str(item.get("description", ""))
        ]
        if skipped:
            return _warn(
                "Q-EX-06",
                "extract",
                "存在因提炼失败而跳过的分块，规则可能不完整",
                counts={"skipped_chunks": len(skipped)},
                object_ids=[
                    str(obj)
                    for item in skipped
                    for obj in item.get("object_ids", [])
                ][:50],
            )
        return _pass("Q-EX-06", "extract", "没有因失败跳过的分块")

    def kind_ratio_check() -> Check:
        if extract_payload is None:
            return _na("Q-EX-07", "extract", "缺少 extract 快照")
        raw_rules = extract_payload.get("rules", [])
        if not raw_rules:
            return _pass("Q-EX-07", "extract", "没有规则需要分类")
        evidence = {
            item["evidence_id"]: m.EvidenceSpan.model_validate(item)
            for item in extract_payload.get("evidence", [])
        }
        rules = [m.RequirementRule.model_validate(item) for item in raw_rules]
        ui_rules = [rule for rule in rules if is_ui_display_rule(rule, evidence)]
        business = [rule for rule in rules if rule not in ui_rules]
        other_business = sum(1 for rule in business if rule.kind == "other")
        ratio = other_business / len(business) if business else 0.0
        counts = {
            "rules": len(rules),
            "ui_display": len(ui_rules),
            "business": len(business),
            "other_business": other_business,
            "ratio": round(ratio, 3),
        }
        if business and ratio > 0.4:
            return _warn(
                "Q-EX-07",
                "extract",
                "排除纯界面规则后 other 仍偏高，建议人工核对分类",
                counts=counts,
            )
        return _pass("Q-EX-07", "extract", "规则分类比例可用", counts=counts)

    return [
        _safe("Q-EX-01", "extract", schema_errors, evidence_check),
        _safe("Q-EX-02", "extract", schema_errors, closure_check),
        _safe("Q-EX-03", "extract", schema_errors, rule_closure),
        _safe("Q-EX-04", "extract", schema_errors, constraint_check),
        _safe("Q-EX-05", "extract", schema_errors, issue_check),
        _safe("Q-EX-06", "extract", schema_errors, skipped_check),
        _safe("Q-EX-07", "extract", schema_errors, kind_ratio_check),
    ]


def _point_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    plan_payload = snapshots.get("plan_points")
    extract_payload = snapshots.get("extract")
    tree_payload = snapshots.get("point_tree")

    def closure_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-01", "plan_points", "缺少 plan_points 快照")
        rules = set(_rules_by_id(extract_payload))
        if extract_payload is None:
            return _na("Q-PT-01", "plan_points", "缺少 extract 快照，无法核对规则引用")
        bad = [
            point.get("point_id", "")
            for point in plan_payload.get("points", [])
            if not point.get("rule_ids")
            or not set(point.get("rule_ids", [])) <= rules
        ]
        if bad:
            return _fail(
                "Q-PT-01",
                "plan_points",
                "测试点引用了不存在的规则或规则为空",
                counts={"points": len(plan_payload.get("points", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-PT-01",
            "plan_points",
            "测试点规则引用闭包完整",
            counts={"points": len(plan_payload.get("points", []))},
        )

    def distribution_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-02", "plan_points", "缺少 plan_points 快照")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        statuses = Counter(point.status for point in points)
        inconsistent = sum(
            point.status == "ready" and point.applicability != "applicable"
            for point in points
        )
        counts = {
            "total": len(points),
            "ready": statuses.get("ready", 0),
            "needs_clarification": statuses.get("needs_clarification", 0),
            "needs_context": statuses.get("needs_context", 0),
            "not_applicable": statuses.get("not_applicable", 0),
            "applicability_mismatch": inconsistent,
        }
        if inconsistent:
            return _warn(
                "Q-PT-02",
                "plan_points",
                "存在 ready 但 applicability 不是 applicable 的测试点",
                counts=counts,
                object_ids=[
                    point.point_id
                    for point in points
                    if point.status == "ready" and point.applicability != "applicable"
                ],
            )
        if not points:
            return _warn("Q-PT-02", "plan_points", "没有测试点", counts=counts)
        if not counts["ready"]:
            return _warn(
                "Q-PT-02",
                "plan_points",
                "没有 ready 测试点，后续无法生成确定用例",
                counts=counts,
            )
        return _pass("Q-PT-02", "plan_points", "测试点状态分布可用", counts=counts)

    def boundary_check() -> Check:
        if plan_payload is None or extract_payload is None:
            return _na("Q-PT-03", "plan_points", "缺少 plan_points/extract 快照")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        missing: list[str] = []
        checked = 0
        for rule in _rules_by_id(extract_payload).values():
            if rule.kind != "field_constraint":
                continue
            values = boundary_values(rule)
            if not values:
                values = qualitative_boundary_values(rule)
            if not values:
                continue
            checked += 1
            candidates = [
                point
                for point in points
                if point.technique == "boundary" and rule.rule_id in point.rule_ids
            ]
            if not all(
                any(value in point.objective for point in candidates) for value in values
            ):
                missing.append(rule.rule_id)
        counts = {"rules_with_boundary": checked, "missing": len(missing)}
        if missing:
            return _fail(
                "Q-PT-03",
                "plan_points",
                "可计算数值约束缺少完整的边界测试点",
                counts=counts,
                object_ids=missing,
            )
        return _pass(
            "Q-PT-03", "plan_points", "数值约束的边界测试点完整", counts=counts
        )

    def missing_boundary_check() -> Check:
        if extract_payload is None:
            return _na("Q-PT-04", "plan_points", "缺少 extract 快照")
        missing = [
            rule.rule_id
            for rule in _rules_by_id(extract_payload).values()
            if rule.kind == "field_constraint"
            and not boundary_values(rule)
            and not qualitative_boundary_values(rule)
        ]
        if missing:
            return _warn(
                "Q-PT-04",
                "plan_points",
                "字段约束没有可计算边界，未生成边界测试点",
                counts={"missing": len(missing)},
                object_ids=missing,
            )
        return _pass("Q-PT-04", "plan_points", "字段约束边界可计算")

    def duplicate_check() -> Check:
        if plan_payload is None:
            return _na("Q-PT-05", "plan_points", "缺少 plan_points 快照")
        seen: set[tuple[tuple[str, ...], str, str]] = set()
        duplicates: list[str] = []
        for item in plan_payload.get("points", []):
            key = (
                tuple(item.get("rule_ids", [])),
                str(item.get("technique", "")),
                str(item.get("objective", "")),
            )
            if key in seen:
                duplicates.append(item.get("point_id", ""))
            seen.add(key)
        if duplicates:
            return _warn(
                "Q-PT-05",
                "plan_points",
                "存在规则/方法/目标完全重复的测试点",
                counts={"duplicates": len(duplicates)},
                object_ids=duplicates,
            )
        return _pass("Q-PT-05", "plan_points", "测试点无完全重复")

    def tree_check() -> Check:
        if plan_payload is None or tree_payload is None:
            return _na("Q-PT-06", "plan_points", "缺少 plan_points/point_tree 快照")
        tree = PointTree.model_validate(tree_payload)
        planned = {
            str(item.get("point_id")) for item in plan_payload.get("points", [])
        }
        in_tree = {node.point_id for node in tree.nodes if node.kind == "point"}
        if planned != in_tree:
            return _fail(
                "Q-PT-06",
                "plan_points",
                "测试点树与保存的测试点事实不一致",
                counts={"points": len(planned), "tree_points": len(in_tree)},
                object_ids=sorted(planned.symmetric_difference(in_tree)),
            )
        return _pass(
            "Q-PT-06",
            "plan_points",
            "测试点树与测试点事实一致",
            counts={"points": len(planned)},
        )

    def ui_display_check() -> Check:
        if plan_payload is None or extract_payload is None:
            return _na("Q-PT-07", "plan_points", "缺少 plan_points/extract 快照")
        manifest = snapshots.get("manifest") or {}
        version = (
            (manifest.get("prompts") or {}).get("plan_points") or {}
        ).get("version")
        if version != "v2":
            return _na("Q-PT-07", "plan_points", "旧运行未使用 v2 规划提示词")
        policy = (
            (run.get("config_snapshot") or {})
            .get("planning", {})
            .get("ui_display_policy", "merge")
        )
        if policy == "keep":
            return _na("Q-PT-07", "plan_points", "配置为 keep，未要求合并")
        points = [m.TestPoint.model_validate(p) for p in plan_payload.get("points", [])]
        rules = _rules_by_id(extract_payload)
        evidence = _evidence_by_id(extract_payload)
        if not any(item.origin == "vision" for item in evidence.values()):
            return _na("Q-PT-07", "plan_points", "没有 vision 证据")
        display = [point for point in points if is_ui_display_point(point, rules, evidence)]
        if not display:
            return _pass("Q-PT-07", "plan_points", "没有纯界面展示点")
        groups: dict[str, list[m.TestPoint]] = {}
        for point in display:
            feature_ids = {
                rules[rule_id].feature_id
                for rule_id in point.rule_ids
                if rule_id in rules
            }
            if len(feature_ids) == 1:
                groups.setdefault(next(iter(feature_ids)), []).append(point)
        fragmented = [
            point for group in groups.values() if len(group) > 1 for point in group
        ]
        non_summary = [
            point
            for point in display
            if "界面展示汇总" not in point.objective
            and not point.objective.startswith("界面展示：")
        ]
        if fragmented or non_summary:
            return _warn(
                "Q-PT-07",
                "plan_points",
                "仍存在未合并或未概括的纯界面展示点",
                counts={
                    "display_points": len(display),
                    "fragmented": len(fragmented),
                    "non_summary": len(non_summary),
                },
                object_ids=sorted(
                    {point.point_id for point in fragmented + non_summary}
                ),
            )
        return _pass(
            "Q-PT-07",
            "plan_points",
            "纯界面展示点已按功能概括",
            counts={"display_points": len(display)},
        )

    return [
        _safe("Q-PT-01", "plan_points", schema_errors, closure_check),
        _safe("Q-PT-02", "plan_points", schema_errors, distribution_check),
        _safe("Q-PT-03", "plan_points", schema_errors, boundary_check),
        _safe("Q-PT-04", "plan_points", schema_errors, missing_boundary_check),
        _safe("Q-PT-05", "plan_points", schema_errors, duplicate_check),
        _safe("Q-PT-06", "plan_points", schema_errors, tree_check),
        _safe("Q-PT-07", "plan_points", schema_errors, ui_display_check),
    ]


def _case_checks(
    snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    cases_payload = snapshots.get("cases")

    def point_closure() -> Check:
        if cases_payload is None:
            return _na("Q-CS-01", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        bad: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if not case.point_ids or not set(case.point_ids) <= points.keys():
                bad.append(case.case_id)
        if bad:
            return _fail(
                "Q-CS-01",
                "cases",
                "用例引用了不存在的测试点",
                counts={"cases": len(cases_payload.get("cases", [])), "invalid": len(bad)},
                object_ids=bad,
            )
        return _pass(
            "Q-CS-01",
            "cases",
            "用例测试点引用闭包完整",
            counts={"cases": len(cases_payload.get("cases", []))},
        )

    def step_closure() -> Check:
        if cases_payload is None:
            return _na("Q-CS-02", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id")
            for item in cases_payload.get("evidence", [])
        }
        bad: list[str] = []
        boundary_mixed: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            allowed = {
                rule_id
                for point_id in case.point_ids
                if point_id in points
                for rule_id in points[point_id].rule_ids
            }
            if len(case.point_ids) > 1 and any(
                point_id in points and points[point_id].technique == "boundary"
                for point_id in case.point_ids
            ):
                boundary_mixed.append(case.case_id)
            invalid = any(
                not step.action.strip()
                or not step.expected.strip()
                or not set(step.rule_ids) <= allowed
                or not set(step.rule_ids) <= set(rules)
                or not set(step.evidence_ids) <= evidence
                for step in case.steps
            )
            if invalid:
                bad.append(case.case_id)
        if bad or boundary_mixed:
            return _fail(
                "Q-CS-02",
                "cases",
                "用例步骤引用越界、动作为空或边界点被合并",
                counts={"invalid": len(bad), "boundary_mixed": len(boundary_mixed)},
                object_ids=sorted(set(bad + boundary_mixed)),
            )
        return _pass("Q-CS-02", "cases", "用例步骤引用闭包完整")

    def coverage_integrity() -> Check:
        if cases_payload is None:
            return _na("Q-CS-03", "cases", "缺少 cases 快照")
        cases = {
            item.get("case_id"): m.TestCase.model_validate(item)
            for item in cases_payload.get("cases", [])
        }
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {rule.get("rule_id") for rule in cases_payload.get("rules", [])}
        bad: list[str] = []
        duplicates: set[tuple[str, int, str, str]] = set()
        for item in cases_payload.get("coverage", []):
            link = m.CoverageLink.model_validate(item)
            key = (link.case_id, link.step_no, link.rule_id, link.point_id)
            case = cases.get(link.case_id)
            point = points.get(link.point_id)
            if (
                case is None
                or point is None
                or link.rule_id not in rules
                or link.rule_id not in point.rule_ids
                or not 1 <= link.step_no <= len(case.steps)
            ):
                bad.append(link.case_id)
            if key in duplicates:
                bad.append(link.case_id)
            duplicates.add(key)
        if bad:
            return _fail(
                "Q-CS-03",
                "cases",
                "覆盖关联引用无效或重复",
                counts={"links": len(cases_payload.get("coverage", [])), "invalid": len(set(bad))},
                object_ids=sorted(set(bad)),
            )
        return _pass(
            "Q-CS-03",
            "cases",
            "覆盖关联引用的用例/步骤/规则/测试点有效",
            counts={"links": len(cases_payload.get("coverage", []))},
        )

    def link_set() -> Check:
        if cases_payload is None:
            return _na("Q-CS-04", "cases", "缺少 cases 快照")
        points = {
            point.get("point_id"): _case_point(point)
            for point in cases_payload.get("points", [])
        }
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id"): m.EvidenceSpan.model_validate(item)
            for item in cases_payload.get("evidence", [])
        }
        expected: set[tuple[str, int, str, str]] = set()
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if not set(case.point_ids) <= points.keys():
                continue
            if any(
                not set(step.rule_ids) <= set(rules)
                or not set(step.evidence_ids) <= set(evidence)
                for step in case.steps
            ):
                continue
            for number, step in enumerate(case.steps, 1):
                for rule_id in step.rule_ids:
                    for point_id in case.point_ids:
                        if rule_id in points[point_id].rule_ids:
                            expected.add((case.case_id, number, rule_id, point_id))
        saved = {
            (
                item.get("case_id", ""),
                int(item.get("step_no", 0)),
                item.get("rule_id", ""),
                item.get("point_id", ""),
            )
            for item in cases_payload.get("coverage", [])
        }
        missing = expected - saved
        extra = saved - expected
        if missing or extra:
            return _warn(
                "Q-CS-04",
                "cases",
                "已保存覆盖关联与重算集合存在差异",
                counts={"missing": len(missing), "extra": len(extra)},
                object_ids=sorted({item[0] for item in missing | extra}),
            )
        return _pass(
            "Q-CS-04",
            "cases",
            "覆盖关联集合与重算结果一致",
            counts={"links": len(expected)},
        )

    def duplicate_cases() -> Check:
        if cases_payload is None:
            return _na("Q-CS-05", "cases", "缺少 cases 快照")
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            signature = case_signature(case)
            if signature in seen:
                duplicates.append(case.case_id)
            seen.add(signature)
        if duplicates:
            return _warn(
                "Q-CS-05",
                "cases",
                "存在步骤和输入完全重复的用例",
                counts={"duplicates": len(duplicates)},
                object_ids=duplicates,
            )
        return _pass("Q-CS-05", "cases", "用例无完全重复")

    def unsupported_expectations() -> Check:
        if cases_payload is None:
            return _na("Q-CS-06", "cases", "缺少 cases 快照")
        rules = {
            rule.get("rule_id"): m.RequirementRule.model_validate(rule)
            for rule in cases_payload.get("rules", [])
        }
        evidence = {
            item.get("evidence_id"): m.EvidenceSpan.model_validate(item)
            for item in cases_payload.get("evidence", [])
        }
        flagged: list[str] = []
        for item in cases_payload.get("cases", []):
            case = m.TestCase.model_validate(item)
            if any(
                unsupported_numbers(step, rules, evidence) for step in case.steps
            ):
                flagged.append(case.case_id)
        if flagged:
            return _warn(
                "Q-CS-06",
                "cases",
                "用例预期含无直接依据的数字，需人工确认",
                counts={"cases": len(flagged)},
                object_ids=flagged,
            )
        return _pass("Q-CS-06", "cases", "未发现无依据的预期数字")

    def status_check() -> Check:
        if cases_payload is None:
            return _na("Q-CS-07", "cases", "缺少 cases 快照")
        statuses = Counter(
            str(item.get("status", "")) for item in cases_payload.get("cases", [])
        )
        counts = {key: value for key, value in statuses.items()}
        if statuses and set(statuses) == {"needs_review"}:
            return _warn(
                "Q-CS-07",
                "cases",
                "全部用例都被自动检查标记为 needs_review",
                counts=counts,
            )
        return _pass("Q-CS-07", "cases", "用例状态分布可用", counts=counts)

    def uncovered_points() -> Check:
        if cases_payload is None:
            return _na("Q-CS-08", "cases", "缺少 cases 快照")
        points = [
            _case_point(item)
            for item in cases_payload.get("points", [])
        ]
        covered = {
            point_id
            for item in cases_payload.get("cases", [])
            for point_id in item.get("point_ids", [])
        }
        missing = [
            point.point_id
            for point in points
            if point.status == "ready"
            and point.applicability == "applicable"
            and point.point_id not in covered
        ]
        if missing:
            return _warn(
                "Q-CS-08",
                "cases",
                "ready 且适用的测试点没有用例覆盖",
                counts={"missing": len(missing)},
                object_ids=missing,
            )
        return _pass("Q-CS-08", "cases", "ready 测试点均有用例覆盖")

    return [
        _safe("Q-CS-01", "cases", schema_errors, point_closure),
        _safe("Q-CS-02", "cases", schema_errors, step_closure),
        _safe("Q-CS-03", "cases", schema_errors, coverage_integrity),
        _safe("Q-CS-04", "cases", schema_errors, link_set),
        _safe("Q-CS-05", "cases", schema_errors, duplicate_cases),
        _safe("Q-CS-06", "cases", schema_errors, unsupported_expectations),
        _safe("Q-CS-07", "cases", schema_errors, status_check),
        _safe("Q-CS-08", "cases", schema_errors, uncovered_points),
    ]


def _retrieval_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    retrieval = snapshots.get("retrieval")
    index = snapshots.get("index")
    run_status = str(run.get("status", ""))

    def required_check() -> Check:
        if retrieval is None or index is None:
            if run_status in {"succeeded", "partial_failed"}:
                return _fail(
                    "Q-RT-01",
                    "retrieval",
                    "运行声称完成但缺少 index/retrieval 快照",
                    counts={
                        "index": int(index is not None),
                        "retrieval": int(retrieval is not None),
                    },
                )
            return _na("Q-RT-01", "retrieval", "运行未完成，缺少 index/retrieval 快照")
        bundles = retrieval.get("bundles", [])
        invalid = [
            str(bundle.get("query_id", ""))
            for bundle in bundles
            if not all(
                bundle.get(key)
                for key in ("query_id", "scope", "space_id", "index_version")
            )
        ]
        version = index.get("index_version")
        if not version or invalid:
            return _fail(
                "Q-RT-01",
                "retrieval",
                "index 或检索记录缺少范围/空间/版本",
                counts={"bundles": len(bundles), "invalid": len(invalid)},
                object_ids=invalid,
            )
        return _pass(
            "Q-RT-01",
            "retrieval",
            "索引与检索记录带完整范围/空间/版本",
            counts={"bundles": len(bundles)},
        )

    def ids_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-02", "retrieval", "缺少 retrieval 快照")
        known = set(_evidence_by_id(snapshots.get("extract")))
        known |= _source_evidence_ids(snapshots.get("chunk"))
        if not known:
            return _na("Q-RT-02", "retrieval", "缺少证据/分块快照，无法核对检索 ID")
        invalid: list[str] = []
        rank_bad: list[str] = []
        for bundle in retrieval.get("bundles", []):
            direct = set(bundle.get("direct_ids", []))
            if not direct <= known:
                invalid.extend(sorted(direct - known))
            supplemental = {
                item.get("evidence_id")
                for item in bundle.get("supplemental_evidence", [])
            }
            if not supplemental <= known:
                invalid.extend(sorted(str(item) for item in supplemental - known))
            for key in ("hits", "fusion"):
                for hit in bundle.get(key, []):
                    object_id = hit.get("object_id")
                    if object_id and object_id not in known:
                        invalid.append(str(object_id))
            invalid.extend(
                str(item)
                for item in bundle.get("keyword_ids", [])
                if item not in known
            )
            ranks = [hit.get("rank") for hit in bundle.get("hits", [])]
            if ranks != list(range(1, len(ranks) + 1)):
                rank_bad.append(str(bundle.get("query_id", "")))
        if invalid or rank_bad:
            return _fail(
                "Q-RT-02",
                "retrieval",
                "检索命中引用了范围外对象或排名不连续",
                counts={"invalid_ids": len(set(invalid)), "bad_ranks": len(rank_bad)},
                object_ids=sorted(set(invalid + rank_bad)),
            )
        return _pass(
            "Q-RT-02",
            "retrieval",
            "检索命中均属于当前证据范围且排名连续",
            counts={"bundles": len(retrieval.get("bundles", []))},
        )

    def adoption_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-03", "retrieval", "缺少 retrieval 快照")
        invalid: list[str] = []
        for bundle in retrieval.get("bundles", []):
            supplemental = {
                item.get("evidence_id")
                for item in bundle.get("supplemental_evidence", [])
            }
            adopted = set(bundle.get("adopted_ids", []))
            omitted = set(bundle.get("omitted_ids", []))
            if not adopted <= supplemental or not omitted <= supplemental:
                invalid.append(str(bundle.get("query_id", "")))
            if adopted & omitted:
                invalid.append(str(bundle.get("query_id", "")))
        if invalid:
            return _fail(
                "Q-RT-03",
                "retrieval",
                "采用/省略证据不是补充材料的合法划分",
                counts={"invalid": len(set(invalid))},
                object_ids=sorted(set(invalid)),
            )
        return _pass("Q-RT-03", "retrieval", "采用/省略证据划分合法")

    def rerank_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-04", "retrieval", "缺少 retrieval 快照")
        statuses = Counter(
            str(bundle.get("rerank", {}).get("status", ""))
            for bundle in retrieval.get("bundles", [])
        )
        invalid = [
            status
            for status in statuses
            if status not in {"disabled", "succeeded", "fallback"}
        ]
        if invalid:
            return _fail(
                "Q-RT-04",
                "retrieval",
                "rerank 状态不在允许枚举内",
                counts={status: statuses[status] for status in statuses},
            )
        if statuses.get("fallback"):
            return _warn(
                "Q-RT-04",
                "retrieval",
                "部分检索发生 rerank 降级，保留召回排名",
                counts={status: statuses[status] for status in statuses},
            )
        return _pass(
            "Q-RT-04",
            "retrieval",
            "rerank 状态合法",
            counts={status: statuses[status] for status in statuses},
        )

    def version_check() -> Check:
        if retrieval is None:
            return _na("Q-RT-05", "retrieval", "缺少 retrieval 快照")
        versions = {
            str(bundle.get("index_version", ""))
            for bundle in retrieval.get("bundles", [])
        }
        if len(versions) > 1:
            return _warn(
                "Q-RT-05",
                "retrieval",
                "同一运行内检索记录使用了不同索引版本",
                counts={"versions": len(versions)},
                object_ids=sorted(versions),
            )
        return _pass(
            "Q-RT-05",
            "retrieval",
            "检索记录索引版本一致",
            counts={"versions": len(versions)},
        )

    return [
        _safe("Q-RT-01", "retrieval", schema_errors, required_check),
        _safe("Q-RT-02", "retrieval", schema_errors, ids_check),
        _safe("Q-RT-03", "retrieval", schema_errors, adoption_check),
        _safe("Q-RT-04", "retrieval", schema_errors, rerank_check),
        _safe("Q-RT-05", "retrieval", schema_errors, version_check),
    ]


def _invocation_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    invocations = snapshots.get("invocations")

    def failed_check() -> Check:
        if invocations is None:
            return _na("Q-IV-01", "invocations", "旧运行缺少调用记录")
        items = invocations.get("items", [])
        failed = [item for item in items if item.get("error") is not None]
        if failed:
            return _warn(
                "Q-IV-01",
                "invocations",
                "存在失败的模型调用，需结合阶段快照判断影响",
                counts={"calls": len(items), "failed": len(failed)},
                object_ids=[str(item.get("invocation_id", "")) for item in failed],
            )
        return _pass(
            "Q-IV-01",
            "invocations",
            "没有失败的模型调用",
            counts={"calls": len(items)},
        )

    def truncated_check() -> Check:
        if invocations is None:
            return _na("Q-IV-02", "invocations", "旧运行缺少调用记录")
        truncated = [
            item
            for item in invocations.get("items", [])
            if item.get("error") == "OutputTruncatedError"
        ]
        if truncated:
            return _warn(
                "Q-IV-02",
                "invocations",
                "存在模型输出截断调用",
                counts={"truncated": len(truncated)},
                object_ids=[str(item.get("invocation_id", "")) for item in truncated],
            )
        return _pass("Q-IV-02", "invocations", "没有输出截断调用")

    def repair_check() -> Check:
        if invocations is None:
            return _na("Q-IV-03", "invocations", "旧运行缺少调用记录")
        repaired = len(invocations.get("validation_errors", {}))
        if repaired:
            return _warn(
                "Q-IV-03",
                "invocations",
                "存在结构修复记录，需核对最终结构",
                counts={"repaired_calls": repaired},
            )
        return _pass("Q-IV-03", "invocations", "没有结构修复记录")

    def usage_check() -> Check:
        if invocations is None:
            return _na("Q-IV-04", "invocations", "旧运行缺少调用记录")
        unknown = 0
        for item in invocations.get("items", []):
            usage = item.get("usage", {})
            if any(
                usage.get(field) is None
                for field in ("input_tokens", "output_tokens", "total_tokens")
            ):
                unknown += 1
        if unknown:
            return _warn(
                "Q-IV-04",
                "invocations",
                "部分调用 usage 未知，不能当作零消耗",
                counts={"unknown_usage": unknown},
            )
        return _pass("Q-IV-04", "invocations", "调用 usage 完整")

    def budget_check() -> Check:
        if invocations is None:
            return _na("Q-IV-05", "invocations", "旧运行缺少调用记录")
        maximum = (
            (run.get("config_snapshot") or {})
            .get("runtime", {})
            .get("budget", {})
            .get("max_calls")
        )
        calls = len(invocations.get("items", []))
        if maximum is None:
            return _pass(
                "Q-IV-05",
                "invocations",
                "未配置调用预算上限",
                counts={"calls": calls},
            )
        if calls >= int(maximum):
            return _warn(
                "Q-IV-05",
                "invocations",
                "实际调用已达到配置预算上限",
                counts={"calls": calls, "max_calls": int(maximum)},
            )
        return _pass(
            "Q-IV-05",
            "invocations",
            "调用次数未达到配置预算上限",
            counts={"calls": calls, "max_calls": int(maximum)},
        )

    return [
        _safe("Q-IV-01", "invocations", schema_errors, failed_check),
        _safe("Q-IV-02", "invocations", schema_errors, truncated_check),
        _safe("Q-IV-03", "invocations", schema_errors, repair_check),
        _safe("Q-IV-04", "invocations", schema_errors, usage_check),
        _safe("Q-IV-05", "invocations", schema_errors, budget_check),
    ]


def _output_checks(
    run: dict[str, Any], snapshots: SnapshotMap, schema_errors: dict[str, str]
) -> list[Check]:
    cases = snapshots.get("cases")
    manifest = snapshots.get("manifest")
    invocations = snapshots.get("invocations")
    failure = snapshots.get("failure")
    run_status = str(run.get("status", ""))

    def self_consistency() -> Check:
        if cases is None:
            return _na("Q-OU-01", "output", "缺少 cases 快照")
        quality = cases.get("quality", {})
        points = [
            _case_point(item) for item in cases.get("points", [])
        ]
        ready = sum(
            point.status == "ready" and point.applicability == "applicable"
            for point in points
        )
        covered = len(
            {item.get("point_id") for item in cases.get("coverage", [])}
        )
        expected = {
            "points": ready,
            "cases": len(cases.get("cases", [])),
            "covered_points": covered,
        }
        actual = {key: quality.get(key) for key in expected}
        if actual != expected:
            return _fail(
                "Q-OU-01",
                "output",
                "cases.quality 计数与快照事实不一致",
                counts={"expected": sum(expected.values()), "actual": sum(
                    value for value in actual.values() if isinstance(value, int)
                )},
                detail={"expected": expected, "actual": actual},
            )
        return _pass(
            "Q-OU-01",
            "output",
            "cases.quality 计数与快照事实一致",
            counts=expected,
        )

    def run_identity() -> Check:
        if cases is None:
            return _na("Q-OU-02", "output", "缺少 cases 快照")
        if str(cases.get("run_id", "")) != str(run.get("run_id", "")):
            return _fail(
                "Q-OU-02",
                "output",
                "cases 快照的 run_id 与运行不一致",
                object_ids=[str(cases.get("run_id", "")), str(run.get("run_id", ""))],
            )
        output = snapshots.get("cases_output") or {}
        if not output.get("directory"):
            return _warn("Q-OU-02", "output", "运行未记录输出目录")
        return _pass(
            "Q-OU-02",
            "output",
            "运行与输出目录已记录",
            object_ids=[str(output.get("directory", ""))],
        )

    def usage_consistency() -> Check:
        if manifest is None or invocations is None:
            return _na("Q-OU-03", "output", "缺少 manifest/invocations 快照")
        items = invocations.get("items", [])
        expected: dict[str, int | None] = {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            values = [item.get("usage", {}).get(field) for item in items]
            expected[field] = (
                sum(value for value in values if value is not None)
                if values and all(value is not None for value in values)
                else None
            )
        actual = manifest.get("metrics", {}).get("usage", {})
        if actual != expected:
            return _warn(
                "Q-OU-03",
                "output",
                "manifest usage 聚合与调用记录不一致",
                detail={"manifest": actual, "invocations": expected},
            )
        return _pass("Q-OU-03", "output", "manifest usage 聚合与调用记录一致")

    def failure_check() -> Check:
        if failure is not None:
            if run_status in {"failed", "cancelled"}:
                return _warn(
                    "Q-OU-04",
                    "output",
                    f"运行状态为 {run_status}，失败阶段已记录",
                    object_ids=[str(failure.get("stage", ""))],
                    detail={"error_type": failure.get("error_type")},
                )
            return _fail(
                "Q-OU-04",
                "output",
                "运行存在失败阶段快照",
                object_ids=[str(failure.get("stage", ""))],
                detail={"error_type": failure.get("error_type")},
            )
        if run_status == "partial_failed":
            return _warn("Q-OU-04", "output", "运行以 partial_failed 结束")
        if run_status in {"failed", "cancelled"}:
            return _warn("Q-OU-04", "output", f"运行状态为 {run_status}")
        return _pass("Q-OU-04", "output", "运行没有失败阶段快照")

    def prompt_check() -> Check:
        if manifest is None:
            return _na("Q-RU-02", "manifest", "缺少 manifest 快照")
        prompts = manifest.get("prompts")
        if not isinstance(prompts, dict) or not prompts:
            return _warn("Q-RU-02", "manifest", "manifest 缺少提示词版本/指纹")
        bad = [
            key
            for key, value in prompts.items()
            if not isinstance(value, dict)
            or not value.get("version")
            or not value.get("fingerprint")
        ]
        if bad:
            return _fail(
                "Q-RU-02",
                "manifest",
                "提示词版本或指纹缺失",
                object_ids=bad,
            )
        return _pass(
            "Q-RU-02",
            "manifest",
            "提示词版本与指纹完整",
            counts={"prompts": len(prompts)},
        )

    def secret_check() -> Check:
        if _contains_api_key(run.get("config_snapshot", {})) or _contains_api_key(
            (manifest or {}).get("config", {})
        ):
            return _fail("Q-RU-03", "manifest", "运行快照包含未排除的 api_key 字段")
        return _pass("Q-RU-03", "manifest", "运行快照未包含 api_key 字段")

    def semantic_check() -> Check:
        if cases is None:
            return _na("Q-SEMANTIC-01", "cases", "缺少 cases 快照")
        value = cases.get("quality", {}).get("semantic_review")
        if value == "not_performed":
            return _pass(
                "Q-SEMANTIC-01",
                "cases",
                "语义审核未执行，自动检查不冒充业务正确性",
            )
        if value is None:
            return _warn(
                "Q-SEMANTIC-01",
                "cases",
                "旧快照未记录 semantic_review，按未审核处理",
            )
        return _fail(
            "Q-SEMANTIC-01",
            "cases",
            "semantic_review 不是 not_performed",
            detail={"value": str(value)},
        )

    return [
        _safe("Q-OU-01", "output", schema_errors, self_consistency),
        _safe("Q-OU-02", "output", schema_errors, run_identity),
        _safe("Q-OU-03", "output", schema_errors, usage_consistency),
        _safe("Q-OU-04", "output", schema_errors, failure_check),
        _safe("Q-RU-02", "manifest", schema_errors, prompt_check),
        _safe("Q-RU-03", "manifest", schema_errors, secret_check),
        _safe("Q-SEMANTIC-01", "cases", schema_errors, semantic_check),
    ]


def inspect_quality(run: dict[str, Any], snapshots: SnapshotMap) -> dict[str, Any]:
    """只读核对已完成运行的阶段快照；不调用模型，不修改数据库。"""
    schema_errors = _schema_errors(snapshots)
    checks: list[Check] = [
        _check(
            "Q-SC-01",
            "snapshot",
            "error" if schema_errors else "info",
            _FAIL if schema_errors else _PASS,
            "阶段快照可解析" if not schema_errors else "阶段快照结构无效",
            counts={"stages": len(snapshots), "invalid_stages": len(schema_errors)},
            object_ids=sorted(schema_errors),
            detail={"errors": schema_errors} if schema_errors else {},
        )
    ]
    checks.extend(_input_checks(run, snapshots, schema_errors))
    checks.extend(_parse_checks(run, snapshots, schema_errors))
    checks.extend(_chunk_checks(run, snapshots, schema_errors))
    checks.extend(_extract_checks(snapshots, schema_errors))
    checks.extend(_point_checks(run, snapshots, schema_errors))
    checks.extend(_case_checks(snapshots, schema_errors))
    checks.extend(_retrieval_checks(run, snapshots, schema_errors))
    checks.extend(_invocation_checks(run, snapshots, schema_errors))
    checks.extend(_output_checks(run, snapshots, schema_errors))

    chunk = snapshots.get("chunk") or {}
    document = chunk.get("document") or {}
    limits = [
        "只核对已保存快照，不重新解析、不调用模型",
        "自动检查通过不等于需求语义正确",
        "不覆盖客户端打开效果、表格列语义和用例可执行性",
        "semantic_review=not_performed；人工结论需另行记录",
    ]
    limits.extend(f"missing_stage:{stage}" for stage in _missing_stages(snapshots))
    return {
        "schema_version": "1.0",
        "run_id": str(run.get("run_id", "")),
        "run_status": str(run.get("status", "")),
        "document": {
            "name": document.get("name"),
            "content_hash": document.get("content_hash"),
        },
        "stages_present": sorted(snapshots),
        "overall": _overall(checks),
        "semantic_review": "not_performed",
        "checks": checks,
        "limits": limits,
    }


_EXPECTED_STAGES = (
    "input",
    "chunk",
    "extract",
    "index",
    "plan_points",
    "cases",
    "retrieval",
    "invocations",
    "manifest",
)


def _missing_stages(snapshots: SnapshotMap) -> list[str]:
    return [stage for stage in _EXPECTED_STAGES if stage not in snapshots]
