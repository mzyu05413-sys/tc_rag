"""检索上下文仅输出需求主线字段，历史审计可读且不丢失原始记录。"""

import pytest
from pydantic import ValidationError

from tc_rag.domain.rag import ContextPayload, RagRecord


@pytest.fixture
def context_payload():
    # 使用最小虚构上下文，不读取真实需求、模型响应或运行数据库。
    return {
        "query_id": "query_fixture",
        "scope": "requirement_fixture",
        "space_id": "space_fixture",
        "index_version": "index_fixture",
        "direct_ids": ["e_fixture"],
        "hits": [],
        "vector_hits": [],
        "keyword_ids": [],
        "fusion": [],
        "rerank": {"status": "disabled"},
        "supplemental_evidence": [],
        "query_embedding_cache_hit": False,
        "timings_ms": {},
        "selection": "fixture",
    }


def test_legacy_context_without_code_audit_fields_remains_readable(context_payload):
    record = RagRecord(record_id="context_fixture", kind="context", payload=context_payload)
    restored = RagRecord.model_validate_json(record.model_dump_json())
    context = ContextPayload.model_validate(restored.payload)
    assert context.code_adopted_ids == []
    assert context.code_omitted_ids == []
    assert "code_adopted_ids" not in context.model_dump()
    assert "code_omitted_ids" not in context.model_dump()


@pytest.mark.parametrize(
    "adopted,omitted",
    [(["source_used"], []), (["source_used"], ["source_trimmed"]), ([], ["source_trimmed"])],
)
def test_legacy_audit_survives_raw_record_but_is_excluded_from_new_context(
    context_payload, adopted, omitted
):
    context_payload.update(code_adopted_ids=adopted, code_omitted_ids=omitted)
    record = RagRecord(record_id="context_fixture", kind="context", payload=context_payload)
    restored = RagRecord.model_validate_json(record.model_dump_json())
    context = ContextPayload.model_validate(restored.payload)
    assert context.code_adopted_ids == adopted
    assert context.code_omitted_ids == omitted
    assert restored.payload == context_payload
    assert "code_adopted_ids" not in context.model_dump(mode="json")
    assert "code_omitted_ids" not in context.model_dump(mode="json")
    assert "code_adopted_ids" not in repr(context)
    # 旧审计仅供回查，不升级成规则证据或重新装配运行支路。
    assert context.direct_ids == ["e_fixture"]


@pytest.mark.parametrize("field", ["code_adopted_ids", "code_omitted_ids"])
def test_code_audit_ids_reject_non_string_elements(context_payload, field):
    context_payload[field] = [7]
    with pytest.raises(ValidationError) as error:
        RagRecord(record_id="context_fixture", kind="context", payload=context_payload)
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        ((field, 0), "string_type")
    ]


def test_context_still_rejects_unrecognized_fields(context_payload):
    context_payload["unknown_code_field"] = []
    with pytest.raises(ValidationError) as error:
        RagRecord(record_id="context_fixture", kind="context", payload=context_payload)
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("unknown_code_field",), "extra_forbidden")
    ]
