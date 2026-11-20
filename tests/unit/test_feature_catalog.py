"""功能目录候选（A1 报告先行）：同名或证据重叠 → 报告候选，不自动合并。"""

from tc_rag.application.services.design.feature_catalog import catalog_feature_candidates
from tc_rag.core.config import Planning
from tc_rag.domain import models as m


def feature(feature_id, name="订单管理", evidence_ids=("e1",)):
    return m.Feature(feature_id=feature_id, name=name, description="",
                     evidence_ids=list(evidence_ids))


def test_same_name_reports_candidate():
    issues = catalog_feature_candidates([
        feature("f1", evidence_ids=["e1"]),
        feature("f2", evidence_ids=["e2"]),
    ])
    assert len(issues) == 1
    assert issues[0].kind == "gap" and issues[0].severity == "info"
    assert issues[0].object_ids == ["f1", "f2"]


def test_shared_evidence_reports_candidate():
    issues = catalog_feature_candidates([
        feature("f1", name="上传", evidence_ids=["e1"]),
        feature("f2", name="校验", evidence_ids=["e1"]),
    ])
    assert len(issues) == 1
    assert issues[0].object_ids == ["f1", "f2"]


def test_distinct_features_report_nothing():
    issues = catalog_feature_candidates([
        feature("f1", name="上传", evidence_ids=["e1"]),
        feature("f2", name="校验", evidence_ids=["e2"]),
    ])
    assert issues == []


def test_planning_config_catalog_report_defaults_off_and_hidden():
    assert Planning().feature_catalog_report is False
    assert "feature_catalog_report" not in Planning().model_dump(mode="json")
    assert Planning(feature_catalog_report=True).model_dump(mode="json")[
        "feature_catalog_report"] is True
