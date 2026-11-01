"""只读质检服务：干净对照与已知缺陷注入，不访问模型或真实数据库。"""

import copy
import json
from typing import Any

from tc_rag.application.services.quality import inspect_quality


def clean_inputs() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    block = {
        "schema_version": "1.0",
        "block_id": "b1",
        "document_id": "doc",
        "kind": "paragraph",
        "heading_path": [],
        "order": 0,
        "content": "最多300个，数量为整数。",
        "locator": {},
        "origin": "text",
    }
    chunk = {
        "schema_version": "1.0",
        "chunk_id": "chunk_1",
        "text": block["content"],
        "token_estimate": len(block["content"]),
        "fingerprint": "chunk_fp",
        "sources": [{"block_id": "b1", "start_char": 0, "end_char": len(block["content"])}],
    }
    evidence = {
        "schema_version": "1.0",
        "evidence_id": "e1",
        "block_id": "b1",
        "locator": {},
        "quote": block["content"],
        "origin": "text",
    }
    feature = {
        "schema_version": "1.0",
        "feature_id": "f1",
        "name": "数量校验",
        "description": "",
        "evidence_ids": ["e1"],
    }
    rule = {
        "schema_version": "1.0",
        "rule_id": "r1",
        "feature_id": "f1",
        "kind": "field_constraint",
        "statement": "最多300个，数量为整数。",
        "structured_constraint": {
            "schema_version": "1.0",
            "field_path": "数量",
            "operator": "<=",
            "boundary": 300,
            "precision": 0,
            "unit": "个",
        },
        "evidence_ids": ["e1"],
        "status": "needs_review",
    }
    points = [
        {
            "schema_version": "1.0",
            "point_id": f"p{index}",
            "rule_ids": ["r1"],
            "technique": "boundary",
            "objective": f"最多300个；边界输入={value}",
            "applicability": "applicable",
            "status": "ready",
        }
        for index, value in enumerate(("299", "300", "301"), 1)
    ]
    tree = {
        "schema_version": "1.0",
        "tree_id": "tree1",
        "version": 1,
        "run_id": "run_demo",
        "document_fingerprint": "hash",
        "nodes": [
            {"node_id": "n0", "title": "需求测试点", "kind": "group", "order": 0},
            *[
                {
                    "node_id": f"n{index}",
                    "parent_id": "n0",
                    "title": point["objective"],
                    "kind": "point",
                    "order": index,
                    "point_id": point["point_id"],
                    "technique": "boundary",
                }
                for index, point in enumerate(points, 1)
            ],
        ],
    }
    cases = [
        {
            "schema_version": "1.0",
            "case_id": f"c{index}",
            "point_ids": [point["point_id"]],
            "title": point["objective"],
            "preconditions": [],
            "data": {},
            "steps": [
                {
                    "schema_version": "1.0",
                    "action": f"输入{value}个",
                    "expected": "最多300个，数量为整数。",
                    "rule_ids": ["r1"],
                    "evidence_ids": ["e1"],
                }
            ],
            "priority": "P2",
            "status": "validated",
        }
        for index, (point, value) in enumerate(zip(points, ("299", "300", "301")), 1)
    ]
    coverage = [
        {
            "schema_version": "1.0",
            "case_id": case["case_id"],
            "step_no": 1,
            "rule_id": "r1",
            "point_id": case["point_ids"][0],
            "validation_status": "needs_review",
        }
        for case in cases
    ]
    run = {
        "run_id": "run_demo",
        "status": "succeeded",
        "config_snapshot": {
            "parsing": {"vision_enabled": False},
            "chunking": {"target_tokens": 900},
            "runtime": {"budget": {"max_calls": 10}},
        },
    }
    snapshots: dict[str, dict[str, Any]] = {
        "input": {"instruction": "生成用例", "document_fingerprint": "hash"},
        "chunk": {
            "document": {
                "schema_version": "1.0",
                "document_id": "doc",
                "name": "fiction.docx",
                "content_hash": "hash",
                "asset_id": "a1",
            },
            "blocks": [block],
            "chunks": [chunk],
            "issues": [],
        },
        "extract": {
            "schema_version": "1.0",
            "rule_mapping": {},
            "evidence": [evidence],
            "features": [feature],
            "rules": [rule],
            "entities": [],
            "issues": [],
        },
        "index": {
            "requirement_set_id": "scope",
            "space_id": "space",
            "manifest_id": "manifest",
            "index_version": "v1",
        },
        "plan_points": {"tree_id": "tree1", "points": points},
        "point_tree": tree,
        "cases": {
            "schema_version": "1.0",
            "run_id": "run_demo",
            "cases": cases,
            "points": [{**point, "category": "字段校验"} for point in points],
            "evidence": [evidence],
            "features": [feature],
            "rules": [rule],
            "coverage": coverage,
            "issues": [],
            "quality": {
                "points": 3,
                "cases": 3,
                "covered_points": 3,
                "semantic_review": "not_performed",
            },
        },
        "issues": {"items": []},
        "retrieval": {
            "bundles": [
                {
                    "query_id": "q1",
                    "scope": "scope",
                    "space_id": "space",
                    "index_version": "v1",
                    "direct_ids": ["e1"],
                    "hits": [
                        {
                            "object_id": "e1",
                            "raw_score": 1.0,
                            "metric": "cosine",
                            "score_kind": "vector",
                            "rank": 1,
                            "index_version": "v1",
                        }
                    ],
                    "vector_hits": [],
                    "keyword_ids": [],
                    "fusion": [],
                    "rerank": {"status": "disabled"},
                    "supplemental_evidence": [],
                    "query_embedding_cache_hit": False,
                    "timings_ms": {"query_embedding_ms": 0.0},
                    "selection": "直接证据不经检索裁剪",
                    "adopted_ids": [],
                    "omitted_ids": [],
                    "generation": {},
                }
            ]
        },
        "invocations": {
            "items": [
                {
                    "schema_version": "1.0",
                    "invocation_id": "inv1",
                    "run_id": "run_demo",
                    "stage": "extract",
                    "provider": "fake",
                    "model": "fake",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                    "timing": {"duration_ms": 1.0},
                    "error": None,
                }
            ],
            "validation_errors": {},
        },
        "manifest": {
            "prompts": {"extract": {"version": "v1", "fingerprint": "fp"}},
            "metrics": {
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                }
            },
            "config": {},
        },
        "cases_output": {"directory": "workdir/output/run_demo"},
    }
    return run, snapshots


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {item["check_id"]: item["status"] for item in report["checks"]}


def test_clean_run_passes():
    run, snapshots = clean_inputs()
    report = inspect_quality(run, snapshots)
    assert report["overall"]["status"] == "pass"
    assert report["semantic_review"] == "not_performed"
    assert not [item for item in report["checks"] if item["status"] in {"fail", "warn"}]


def test_q_pa_01_flags_image_issue_left_after_vision():
    run, snapshots = clean_inputs()
    run["config_snapshot"]["parsing"]["vision_enabled"] = True
    image = {
        "schema_version": "1.0",
        "block_id": "img1",
        "document_id": "doc",
        "kind": "image",
        "heading_path": [],
        "order": 1,
        "content": json.dumps({"asset_id": "a1"}, ensure_ascii=False),
        "locator": {"paragraph_index": 1},
        "origin": "text",
    }
    derived = {
        "schema_version": "1.0",
        "block_id": "img1_vision_fixture",
        "document_id": "doc",
        "kind": "paragraph",
        "heading_path": [],
        "order": 2,
        "content": "图片解释",
        "locator": {"paragraph_index": 1},
        "origin": "vision",
        "processing_model": "fixture",
        "processing_version": "v1",
    }
    base_block = snapshots["chunk"]["blocks"][0]
    snapshots["chunk"]["blocks"] = [base_block, image, derived]
    snapshots["issues"] = {
        "items": [
            {
                "schema_version": "1.0",
                "issue_id": "i1",
                "kind": "unparsed",
                "severity": "warning",
                "description": "图片 a1 仅提取与定位，图片文字尚未解析",
                "object_ids": ["doc"],
            }
        ]
    }
    report = inspect_quality(run, snapshots)
    check = next(item for item in report["checks"] if item["check_id"] == "Q-PA-01")
    assert check["status"] == "fail"
    assert check["counts"]["stale_unparsed"] == 1
    assert check["object_ids"] == ["i1"]


def test_skipped_chunk_and_other_ratio_warn():
    run, snapshots = clean_inputs()
    snapshots["extract"]["issues"] = [
        {
            "schema_version": "1.0",
            "issue_id": "skip",
            "kind": "other",
            "severity": "warning",
            "description": "分块提炼失败，已跳过",
            "object_ids": ["chunk"],
        }
    ]
    snapshots["extract"]["rules"].append(
        {
            **snapshots["extract"]["rules"][0],
            "rule_id": "r2",
            "kind": "other",
            "statement": "纯界面展示",
        }
    )
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-EX-06"] == "warn"
    assert statuses(report)["Q-EX-07"] == "warn"


def test_wrong_quote_fails_evidence_check():
    run, snapshots = clean_inputs()
    snapshots["extract"]["evidence"][0]["quote"] = "原文不存在"
    report = inspect_quality(run, snapshots)
    assert report["overall"]["status"] == "fail"
    assert statuses(report)["Q-EX-01"] == "fail"


def test_dangling_point_rule_fails():
    run, snapshots = clean_inputs()
    snapshots["plan_points"]["points"][0]["rule_ids"] = ["missing"]
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-PT-01"] == "fail"


def test_missing_boundary_point_fails():
    run, snapshots = clean_inputs()
    snapshots["plan_points"]["points"][2]["objective"] = "最多300个；边界输入=302"
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-PT-03"] == "fail"


def test_step_rule_out_of_scope_fails():
    run, snapshots = clean_inputs()
    snapshots["cases"]["cases"][0]["steps"][0]["rule_ids"] = ["missing"]
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-CS-02"] == "fail"


def test_duplicate_case_warns():
    run, snapshots = clean_inputs()
    duplicate = copy.deepcopy(snapshots["cases"]["cases"][0])
    duplicate["case_id"] = "c4"
    snapshots["cases"]["cases"].append(duplicate)
    snapshots["cases"]["quality"]["cases"] = 4
    snapshots["cases"]["coverage"].append(
        {
            "schema_version": "1.0",
            "case_id": "c4",
            "step_no": 1,
            "rule_id": "r1",
            "point_id": "p1",
            "validation_status": "needs_review",
        }
    )
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-CS-05"] == "warn"
    assert report["overall"]["status"] == "warn"


def test_unsupported_expectation_number_warns():
    run, snapshots = clean_inputs()
    snapshots["cases"]["cases"][0]["steps"][0]["expected"] = "最多300个，数量为整数，42次"
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-CS-06"] == "warn"


def test_invalid_chunk_range_fails():
    run, snapshots = clean_inputs()
    snapshots["chunk"]["chunks"][0]["sources"][0]["end_char"] = 10_000
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-CH-01"] == "fail"


def test_chunk_text_mismatch_fails():
    run, snapshots = clean_inputs()
    snapshots["chunk"]["chunks"][0]["text"] = "被篡改的分块"
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-CH-02"] == "fail"


def test_direct_id_outside_scope_fails():
    run, snapshots = clean_inputs()
    snapshots["retrieval"]["bundles"][0]["direct_ids"].append("missing")
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-RT-02"] == "fail"


def test_failed_invocation_warns():
    run, snapshots = clean_inputs()
    failed = copy.deepcopy(snapshots["invocations"]["items"][0])
    failed["invocation_id"] = "inv2"
    failed["error"] = "HTTP_500"
    snapshots["invocations"]["items"].append(failed)
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-IV-01"] == "warn"


def test_missing_required_stage_fails():
    run, snapshots = clean_inputs()
    del snapshots["retrieval"]
    del snapshots["index"]
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-RT-01"] == "fail"


def test_missing_invocations_is_not_applicable():
    run, snapshots = clean_inputs()
    del snapshots["invocations"]
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-IV-01"] == "not_applicable"
    assert report["overall"]["status"] == "pass"


def test_malformed_snapshot_fails_without_crashing():
    run, snapshots = clean_inputs()
    snapshots["cases"]["cases"] = [{"case_id": "broken"}]
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-SC-01"] == "fail"
    assert statuses(report)["Q-CS-01"] == "not_applicable"


def test_semantic_review_cannot_be_marked_approved():
    run, snapshots = clean_inputs()
    snapshots["cases"]["quality"]["semantic_review"] = "approved"
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-SEMANTIC-01"] == "fail"


def test_api_key_in_snapshot_fails():
    run, snapshots = clean_inputs()
    run["config_snapshot"]["models"] = {"llm": {"api_key": "secret"}}
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-RU-03"] == "fail"


def ui_display_snapshot(points: list[dict[str, Any]]):
    run, snapshots = clean_inputs()
    snapshots["manifest"]["prompts"]["plan_points"] = {
        "version": "v2",
        "fingerprint": "fp",
    }
    snapshots["chunk"]["blocks"][0].update(
        origin="vision", processing_model="fixture", processing_version="v1"
    )
    snapshots["extract"]["evidence"][0].update(
        origin="vision",
        processing_model="fixture",
        processing_version="v1",
        uncertain=True,
    )
    snapshots["extract"]["rules"][0].update(
        kind="other",
        structured_constraint=None,
        statement="产品列表展示字段A、字段B",
    )
    snapshots["plan_points"]["points"] = points
    return run, snapshots


def display_point(point_id: str, objective: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "point_id": point_id,
        "rule_ids": ["r1"],
        "technique": "other",
        "objective": objective,
        "applicability": "applicable",
        "status": "ready",
    }


def test_quality_warns_for_unmerged_ui_display_points():
    run, snapshots = ui_display_snapshot(
        [
            display_point("p1", "字段A、字段B展示"),
            display_point("p2", "列C、列D展示"),
        ]
    )
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-PT-07"] == "warn"


def test_quality_passes_for_ui_display_summary():
    run, snapshots = ui_display_snapshot(
        [display_point("p1", "产品列表：界面展示汇总（合并 2 个纯界面展示点）")]
    )
    report = inspect_quality(run, snapshots)
    assert statuses(report)["Q-PT-07"] == "pass"


def test_quality_ui_display_check_is_not_applicable_for_keep_or_legacy():
    run, snapshots = ui_display_snapshot([display_point("p1", "字段A展示")])
    run["config_snapshot"]["planning"] = {"ui_display_policy": "keep"}
    assert statuses(inspect_quality(run, snapshots))["Q-PT-07"] == "not_applicable"

    run, snapshots = ui_display_snapshot([display_point("p1", "字段A展示")])
    snapshots["manifest"]["prompts"]["plan_points"] = {
        "version": "v1",
        "fingerprint": "fp",
    }
    assert statuses(inspect_quality(run, snapshots))["Q-PT-07"] == "not_applicable"
