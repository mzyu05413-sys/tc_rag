"""按批次限定用例测试点引用，保持持久化结构与旧 CaseBatch 一致。"""

import pytest
from pydantic import ValidationError

from tc_rag.domain.identity import fingerprint
from tc_rag.domain.pipeline import CaseBatch, case_batch_schema


@pytest.fixture
def case_payload():
    return {
        "cases": [
            {
                "case_id": "case_fixture",
                "point_ids": ["point_allowed"],
                "title": "fixture",
                "steps": [
                    {
                        "action": "fixture",
                        "expected": "fixture",
                        "rule_ids": ["rule_fixture"],
                        "evidence_ids": ["evidence_fixture"],
                    }
                ],
            }
        ]
    }


def test_scoped_case_schema_preserves_standard_case_batch_json(case_payload):
    schema = case_batch_schema(["point_allowed"])
    result = schema.model_validate(case_payload)
    expected = CaseBatch.model_validate(case_payload)
    assert result.model_dump(mode="json") == expected.model_dump(mode="json")
    assert CaseBatch.model_validate_json(result.model_dump_json()) == expected
    assert schema().cases == []


@pytest.mark.parametrize("point_ids", [["point_unknown"], ["point_allowed", "point_unknown"], []])
def test_scoped_case_schema_rejects_foreign_or_empty_point_ids(case_payload, point_ids):
    case_payload["cases"][0]["point_ids"] = point_ids
    with pytest.raises(ValidationError):
        case_batch_schema(["point_allowed"]).model_validate(case_payload)


def test_scoped_case_schema_still_rejects_unknown_fields(case_payload):
    case_payload["cases"][0]["unknown_field"] = "fixture"
    with pytest.raises(ValidationError) as error:
        case_batch_schema(["point_allowed"]).model_validate(case_payload)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_scoped_schema_fingerprint_binds_exact_batch():
    first = case_batch_schema(["point_a", "point_b"]).model_json_schema()
    reordered = case_batch_schema(["point_b", "point_a"]).model_json_schema()
    second = case_batch_schema(["point_c"]).model_json_schema()
    assert first == reordered
    assert fingerprint(first) != fingerprint(second)
    # 旧的通用 Schema 缓存不能作为新批次契约的输出直接复用。
    assert fingerprint(first) != fingerprint(CaseBatch.model_json_schema())
    assert first["$defs"]["BatchPointId"]["enum"] == ["point_a", "point_b"]


def test_scoped_case_schema_requires_nonempty_batch():
    with pytest.raises(ValueError):
        case_batch_schema([])


def test_scoped_case_schema_rejects_single_point_overflow_below_batch_cap(case_payload):
    # 复现线上 5 个点、15 条用例，其中一个点 8 条；总数未达 30 仍须拒绝。
    counts = [1, 1, 8, 2, 3]
    case = case_payload["cases"][0]
    case_payload["cases"] = [
        {**case, "case_id": f"c{point}_{index}", "point_ids": [f"p{point}"]}
        for point, count in enumerate(counts)
        for index in range(count)
    ]
    schema = case_batch_schema([f"p{i}" for i in range(5)], 6)
    batch_max = schema.model_json_schema()["properties"]["cases"]["maxItems"]
    assert len(case_payload["cases"]) == 15 < batch_max
    with pytest.raises(ValidationError, match="per-point case count"):
        schema.model_validate(case_payload)


def test_case_count_limit_counts_multi_point_cases_and_repeated_references(case_payload):
    case = case_payload["cases"][0]
    case_payload["cases"] = [
        {**case, "case_id": f"c{i}", "point_ids": ["p0", "p0", "p1"]} for i in range(6)
    ]
    schema = case_batch_schema(["p0", "p1"], 6)
    assert len(schema.model_validate(case_payload).cases) == 6
    case_payload["cases"].append({**case, "case_id": "overflow", "point_ids": ["p1"]})
    with pytest.raises(ValidationError, match="per-point case count"):
        schema.model_validate(case_payload)


def test_case_limit_changes_schema_fingerprint_and_exposes_batch_max():
    legacy = case_batch_schema(["p0", "p1"]).model_json_schema()
    limited = case_batch_schema(["p0", "p1"], 6).model_json_schema()
    smaller = case_batch_schema(["p0", "p1"], 3).model_json_schema()
    assert limited["properties"]["cases"]["maxItems"] == 12
    assert len({fingerprint(s) for s in [legacy, limited, smaller]}) == 3


@pytest.mark.parametrize("limit", [0, -1])
def test_case_limit_must_be_positive(limit):
    with pytest.raises(ValueError, match="正整数"):
        case_batch_schema(["point"], limit)
