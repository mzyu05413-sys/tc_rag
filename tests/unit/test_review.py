"""验证测试点树和生成请求；历史审核快照不参与当前生成流程。"""

import pytest
from pydantic import ValidationError

from tc_rag.application.requests import CasesRequest, PointsRequest
from tc_rag.domain.models import TestCase
from tc_rag.domain.review import PointTree


def tree_data():
    return {
        "tree_id": "tree",
        "version": 1,
        "run_id": "run",
        "document_fingerprint": "doc-hash",
        "nodes": [
            {"node_id": "root", "title": "虚构模块", "kind": "group"},
            {
                "node_id": "leaf",
                "parent_id": "root",
                "title": "虚构测试点",
                "kind": "point",
                "point_id": "point",
            },
        ],
    }


def test_requests_and_roundtrip():
    request = PointsRequest(document_path="demo.pdf", instruction="生成测试点", output_dir="out")
    assert "mode" not in request.model_dump()
    assert PointsRequest.model_validate_json(request.model_dump_json()) == request
    # 改名后稳定 ID 保持不变，后续回导可以据此识别更新。
    data = tree_data()
    data["nodes"][1]["title"] = "修改后的标题"
    tree = PointTree.model_validate(data)
    assert tree.nodes[1].point_id == "point"
    assert PointTree.model_validate_json(tree.model_dump_json()) == tree


def test_cases_request_needs_no_review_or_xmind_input():
    request = CasesRequest(run_id="run", instruction="生成用例", output_dir="out")
    assert set(request.model_dump()) == {
        "schema_version",
        "run_id",
        "instruction",
        "output_dir",
        "policy",
    }
    with pytest.raises(ValidationError):
        CasesRequest.model_validate({**request.model_dump(), "review_id": "obsolete"})


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "missing_parent",
        "point_parent",
        "cycle",
        "two_roots",
        "missing_point",
        "duplicate_point",
    ],
)
def test_bad_tree(fault):
    data = tree_data()
    if fault == "duplicate":
        data["nodes"].append(data["nodes"][1].copy())
    elif fault == "missing_parent":
        data["nodes"][1]["parent_id"] = "missing"
    elif fault == "point_parent":
        data["nodes"].append(
            {"node_id": "child", "parent_id": "leaf", "title": "组", "kind": "group"}
        )
    elif fault == "cycle":
        data["nodes"] += [
            {"node_id": "a", "parent_id": "b", "kind": "group", "title": "a"},
            {"node_id": "b", "parent_id": "a", "kind": "group", "title": "b"},
        ]
    elif fault == "two_roots":
        data["nodes"][1]["parent_id"] = None
    elif fault == "missing_point":
        data["nodes"][1]["point_id"] = None
    else:
        data["nodes"].append({**data["nodes"][1], "node_id": "another"})
    with pytest.raises(ValidationError):
        PointTree.model_validate(data)


def test_case_metadata_compatible_with_old_payload():
    data = {
        "case_id": "c",
        "point_ids": ["p"],
        "title": "虚构用例",
        "steps": [{"action": "执行", "expected": "结果", "rule_ids": ["r"], "evidence_ids": ["e"]}],
    }
    assert TestCase.model_validate(data).external_id is None
    case = TestCase.model_validate({**data, "module_path": "/演示/模块", "product_version": "v1"})
    assert case.edit_mode == "STEP"
    with pytest.raises(ValidationError):
        TestCase.model_validate({**data, "module_path": "错误路径"})
