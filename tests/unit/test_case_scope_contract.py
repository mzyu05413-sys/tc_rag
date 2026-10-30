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
