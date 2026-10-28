import ast
import json
from itertools import combinations
from pathlib import Path

import pytest
from pydantic import ValidationError

from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.pipeline import Extraction


def examples():
    locator = m.Locator(page=1)
    return [
        m.Asset(asset_id="a", relative_path="demo.docx", checksum="hash", mime_type="docx"),
        m.Document(document_id="d", name="demo", content_hash="hash", asset_id="a"),
        m.DocumentBlock(
            block_id="b",
            document_id="d",
            kind="paragraph",
            order=0,
            content="amount > 0",
            locator=locator,
        ),
        m.Chunk(
            chunk_id="c",
            text="amount > 0",
            token_estimate=5,
            fingerprint="hash",
            sources=[m.ChunkSource(block_id="b", start_char=0, end_char=10)],
        ),
        m.EvidenceSpan(evidence_id="e", block_id="b", locator=locator, quote="amount > 0"),
        m.Feature(feature_id="f", name="demo", description="demo", evidence_ids=["e"]),
        m.RequirementRule(
            rule_id="r",
            feature_id="f",
            kind="field_constraint",
            statement="amount > 0",
            evidence_ids=["e"],
        ),
        m.TestPoint(point_id="p", rule_ids=["r"], technique="boundary", objective="zero"),
        m.TestCase(
            case_id="t",
            point_ids=["p"],
            title="demo",
            steps=[
                m.TestStep(
                    action="submit zero", expected="rejected", rule_ids=["r"], evidence_ids=["e"]
                )
            ],
        ),
        m.CoverageLink(case_id="t", step_no=1, rule_id="r", point_id="p"),
        m.Issue(issue_id="i", kind="gap", severity="warning", description="demo"),
        m.Run(run_id="run", config_snapshot={}, status="queued"),
        m.ModelInvocation(
            invocation_id="inv", run_id="run", stage="extract", provider="fake", model="fake"
        ),
        m.BusinessEntity(entity_id="entity", kind="field", name="amount", evidence_ids=["e"]),
    ]


@pytest.mark.parametrize("obj", examples(), ids=lambda obj: type(obj).__name__)
def test_json_roundtrip(obj):
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    assert json.loads(obj.model_dump_json())["schema_version"] == "1.0"


EXTRACTION_COLLECTIONS = [
    ("evidence", "evidence_id", m.EvidenceSpan),
    ("features", "feature_id", m.Feature),
    ("rules", "rule_id", m.RequirementRule),
    ("entities", "entity_id", m.BusinessEntity),
    ("issues", "issue_id", m.Issue),
]


@pytest.mark.parametrize("collection,key,model", EXTRACTION_COLLECTIONS)
def test_extraction_rejects_duplicate_ids_within_every_collection(collection, key, model):
    obj = next(obj for obj in examples() if isinstance(obj, model)).model_dump(mode="json")
    with pytest.raises(ValidationError, match="IDs must be unique"):
        Extraction.model_validate({collection: [obj, dict(obj)]})


@pytest.mark.parametrize("left,right", list(combinations(EXTRACTION_COLLECTIONS, 2)))
def test_extraction_allows_cross_collection_id_reuse(left, right):
    """不同对象类型允许复用同一个本地 ID；引用字段本身已区分类型。"""
    payload = {}
    for collection, key, model in (left, right):
        obj = next(obj for obj in examples() if isinstance(obj, model)).model_dump(mode="json")
        obj[key] = "same-local-id"
        payload[collection] = [obj]
    result = Extraction.model_validate(payload)
    dumped = result.model_dump(mode="json")
    assert dumped[left[0]][0][left[1]] == "same-local-id"
    assert dumped[right[0]][0][right[1]] == "same-local-id"


def test_extraction_preserves_unique_ids_and_reference_values():
    payload = {
        collection: [obj.model_dump(mode="json") for obj in examples() if isinstance(obj, model)]
        for collection, _, model in EXTRACTION_COLLECTIONS
    }
    result = Extraction.model_validate(payload)
    assert result.rules[0].feature_id == result.features[0].feature_id
    assert result.rules[0].evidence_ids == [result.evidence[0].evidence_id]
    assert Extraction.model_validate_json(result.model_dump_json()) == result
    assert Extraction().evidence == []


@pytest.mark.parametrize(
    "obj,field",
    [
        (examples()[2], "kind"),
        (examples()[4], "origin"),
        (examples()[6], "kind"),
        (examples()[6], "status"),
        (examples()[7], "status"),
        (examples()[7], "applicability"),
        (examples()[8], "status"),
        (examples()[11], "status"),
    ],
)
def test_invalid_enum(obj, field):
    data = obj.model_dump()
    data[field] = "invalid"
    with pytest.raises(ValidationError):
        type(obj).model_validate(data)


def test_source_intervals():
    with pytest.raises(ValidationError):
        m.ChunkSource(block_id="b", start_char=5, end_char=5)
    with pytest.raises(ValidationError):
        m.Locator(page=0)


def test_transformed_evidence_requires_provenance():
    data = examples()[4].model_dump()
    data["origin"] = "vision"
    with pytest.raises(ValidationError):
        m.EvidenceSpan.model_validate(data)
    data.update(processing_model="fake", processing_version="v1")
    assert m.EvidenceSpan.model_validate(data).origin == "vision"


def test_steps_require_evidence_and_unknown_usage_stays_unknown():
    data = examples()[8].model_dump()
    data["steps"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        m.TestCase.model_validate(data)
    assert m.ModelInvocation.model_validate(examples()[12].model_dump()).usage.total_tokens is None


def test_fingerprint_and_identity():
    assert fingerprint({"b": 2, "a": "中文"}) == fingerprint({"a": "中文", "b": 2})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert fingerprint([1, 2]) != fingerprint([2, 1])
    assert fingerprint(examples()[0]) == fingerprint(examples()[0].model_dump(mode="json"))
    assert new_id("rule") != new_id("rule")
    with pytest.raises(ValueError):
        fingerprint(float("nan"))
    with pytest.raises(ValueError):
        new_id("bad prefix")


def test_domain_has_no_external_io_imports():
    allowed = {"typing", "pydantic", "hashlib", "json", "re", "uuid"}
    for path in Path("src/tc_rag/domain").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # 允许领域模块内部引用，禁止反向依赖应用层和基础设施层。
                assert node.module.split(".")[0] in allowed or node.module.startswith(
                    "tc_rag.domain."
                )
