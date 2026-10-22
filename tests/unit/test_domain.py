import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tc_rag.domain import models as m
from tc_rag.domain.identity import fingerprint, new_id


def examples():
    locator = m.Locator(page=1)
    return [
        m.Asset(asset_id="a", relative_path="demo.docx", checksum="hash", mime_type="docx"),
        m.Document(document_id="d", name="demo", content_hash="hash", asset_id="a"),
        m.DocumentBlock(block_id="b", document_id="d", kind="paragraph", order=0,
                        content="amount > 0", locator=locator),
        m.Chunk(chunk_id="c", text="amount > 0", token_estimate=5, fingerprint="hash",
                sources=[m.ChunkSource(block_id="b", start_char=0, end_char=10)]),
        m.EvidenceSpan(evidence_id="e", block_id="b", locator=locator, quote="amount > 0"),
        m.Feature(feature_id="f", name="demo", description="demo", evidence_ids=["e"]),
        m.RequirementRule(rule_id="r", feature_id="f", kind="field_constraint",
                          statement="amount > 0", evidence_ids=["e"]),
        m.TestPoint(point_id="p", rule_ids=["r"], technique="boundary", objective="zero"),
        m.TestCase(case_id="t", point_ids=["p"], title="demo", steps=[m.TestStep(
            action="submit zero", expected="rejected", rule_ids=["r"], evidence_ids=["e"])]),
        m.CoverageLink(case_id="t", step_no=1, rule_id="r", point_id="p"),
        m.Issue(issue_id="i", kind="gap", severity="warning", description="demo"),
        m.Run(run_id="run", config_snapshot={}, status="queued"),
        m.ModelInvocation(invocation_id="inv", run_id="run", stage="extract",
                          provider="fake", model="fake"),
        m.BusinessEntity(entity_id="entity", kind="field", name="amount", evidence_ids=["e"]),
    ]


@pytest.mark.parametrize("obj", examples(), ids=lambda obj: type(obj).__name__)
def test_json_roundtrip(obj):
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    assert json.loads(obj.model_dump_json())["schema_version"] == "1.0"


@pytest.mark.parametrize("obj,field", [
    (examples()[2], "kind"), (examples()[4], "origin"), (examples()[6], "kind"),
    (examples()[6], "status"), (examples()[7], "status"), (examples()[7], "applicability"),
    (examples()[8], "status"), (examples()[11], "status"),
])
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
                assert node.module.split(".")[0] in allowed
